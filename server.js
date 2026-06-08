import express from "express";
import { Client } from "@modelcontextprotocol/sdk/client/index.js";
import { StreamableHTTPClientTransport } from "@modelcontextprotocol/sdk/client/streamableHttp.js";
import OpenAI from "openai";
import * as dotenv from "dotenv";

dotenv.config();

const app = express();
app.use(express.json());
app.use(express.static("public"));

// ── MCP + OpenAI setup ────────────────────────────────────────────────────

class MCPClient {
  constructor(serverUrl) {
    this.serverUrl = serverUrl;
    this.client = null;
  }

  async connect() {
    const transport = new StreamableHTTPClientTransport(new URL(this.serverUrl));
    this.client = new Client({ name: "web-client", version: "1.0.0" });
    await this.client.connect(transport);
  }

  async listTools() {
    return (await this.client.listTools()).tools;
  }

  async callTool(name, input) {
    return await this.client.callTool({ name, arguments: input });
  }

  async listResources() {
    const result = await this.client.listResources();
    return result.resources;
  }

  async readResource(uri) {
    const result = await this.client.readResource({ uri });
    return result.contents;
  }
}

const mcpClient = new MCPClient(process.env.MCP_SERVER_URL ?? "http://localhost:8000/mcp");
await mcpClient.connect();
console.log("✅ Connected to MCP server");

const LLM_BASE_URL = process.env.LOCAL_LLM_BASE_URL ?? "http://localhost:11434/v1";

const openai = new OpenAI({
  baseURL: LLM_BASE_URL,
  apiKey: "ollama",
});

// ── Dynamic model selection ───────────────────────────────────────────────
let currentModel = process.env.LLM_MODEL ?? "qwen2.5:7b";

console.log("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━");
console.log("🤖 LLM Model     :", currentModel);
console.log("🌐 LLM Base URL  :", LLM_BASE_URL);
console.log("🔌 MCP Server    :", process.env.MCP_SERVER_URL ?? "http://localhost:8000/mcp");
console.log("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━");

// Conversation history per session (simple in-memory)
const sessions = {};

// ── Model endpoints ───────────────────────────────────────────────────────

app.get("/models", async (req, res) => {
  try {
    const response = await fetch(`${LLM_BASE_URL}/models`);
    const data = await response.json();
    const models = (data.data ?? data.models ?? []).map(m => m.id ?? m.name);
    res.json({ models, current: currentModel });
  } catch (err) {
    res.json({ models: [currentModel], current: currentModel, error: err.message });
  }
});

app.get("/model", (req, res) => {
  res.json({ model: currentModel });
});

app.post("/model", (req, res) => {
  const { model } = req.body;
  if (!model) return res.status(400).json({ error: "model required" });
  currentModel = model;
  console.log(`[Model] Switched to: ${currentModel}`);
  res.json({ model: currentModel });
});

// ── System prompt ─────────────────────────────────────────────────────────

const SYSTEM_PROMPT = `You are a network intelligence agent with access to Forward Networks tools.

IMPORTANT RULES:
- To show data as a graph, call the data tool directly with output_format='graph'
  Example: get_device_basic_info(network_id='123', output_format='graph')
- Do NOT call generate_graph with a query string — it needs real JSON data
- Always call list_networks first to get a valid network_id before any other tool
- Never invent or guess network IDs — always look them up first
- For device inventory queries, use get_device_basic_info with the real network_id
- For hardware EOL queries, use get_hardware_support with the real network_id
`;

// ── Chat endpoint (SSE streaming to browser) ──────────────────────────────

app.post("/chat", async (req, res) => {
  const { message, sessionId } = req.body;

  if (!sessions[sessionId]) {
    sessions[sessionId] = [{ role: "system", content: SYSTEM_PROMPT }];
  }
  const messages = sessions[sessionId];
  messages.push({ role: "user", content: message });

  res.setHeader("Content-Type", "text/event-stream");
  res.setHeader("Cache-Control", "no-cache");
  res.flushHeaders();

  const send = (data) => res.write(`data: ${JSON.stringify(data)}\n\n`);

  // Tell browser which model is being used
  send({ type: "model", model: currentModel });

  const mcpTools = await mcpClient.listTools();
  const tools = mcpTools.map(t => ({
    type: "function",
    function: { name: t.name, description: t.description, parameters: t.inputSchema },
  }));

  try {
    while (true) {
      const stream = await openai.chat.completions.create({
        model: currentModel,   // ← uses dynamic model
        max_tokens: 8000,
        tools,
        tool_choice: "auto",
        messages,
        stream: true,
      });

      let fullContent = "";
      let toolCalls = {};

      for await (const chunk of stream) {
        const delta = chunk.choices[0]?.delta;
        const finishReason = chunk.choices[0]?.finish_reason;

        if (delta?.content) {
          fullContent += delta.content;
          send({ type: "text", content: delta.content });
        }

        if (delta?.tool_calls) {
          for (const tc of delta.tool_calls) {
            const idx = tc.index;
            if (!toolCalls[idx]) toolCalls[idx] = { id: "", name: "", args: "" };
            if (tc.id) toolCalls[idx].id = tc.id;
            if (tc.function?.name) toolCalls[idx].name += tc.function.name;
            if (tc.function?.arguments) toolCalls[idx].args += tc.function.arguments;
          }
        }

        if (finishReason === "tool_calls") {
          messages.push({
            role: "assistant",
            content: fullContent || null,
            tool_calls: Object.values(toolCalls).map(tc => ({
              id: tc.id,
              type: "function",
              function: { name: tc.name, arguments: tc.args },
            })),
          });

          for (const tc of Object.values(toolCalls)) {
            send({ type: "tool_call", name: tc.name, args: JSON.parse(tc.args) });
            const result = await mcpClient.callTool(tc.name, JSON.parse(tc.args));
            const content = result.content.filter(c => c.type === "text").map(c => c.text);
            send({ type: "tool_result", name: tc.name, result: content });
            messages.push({ role: "tool", tool_call_id: tc.id, content: JSON.stringify(content) });
          }

          toolCalls = {};
          break;
        }

        if (finishReason === "stop") {
          messages.push({ role: "assistant", content: fullContent });
          send({ type: "done" });
          res.end();
          return;
        }
      }
    }
  } catch (err) {
    send({ type: "error", message: err.message });
    res.end();
  }
});

// ── Command endpoint ──────────────────────────────────────────────────────

app.post("/command", async (req, res) => {
  const { command, docId, sessionId } = req.body;

  if (!sessions[sessionId]) {
    sessions[sessionId] = [{ role: "system", content: SYSTEM_PROMPT }];
  }
  const messages = sessions[sessionId];

  res.setHeader("Content-Type", "text/event-stream");
  res.setHeader("Cache-Control", "no-cache");
  res.flushHeaders();

  const send = (data) => res.write(`data: ${JSON.stringify(data)}\n\n`);

  try {
    const promptMessages = await mcpClient.client.getPrompt({
      name: command,
      arguments: { doc_id: docId },
    });

    for (const pm of promptMessages.messages) {
      messages.push({
        role: pm.role,
        content: typeof pm.content === "object" ? pm.content.text : pm.content,
      });
    }

    const mcpTools = await mcpClient.listTools();
    const tools = mcpTools.map(t => ({
      type: "function",
      function: { name: t.name, description: t.description, parameters: t.inputSchema },
    }));

    while (true) {
      const stream = await openai.chat.completions.create({
        model: currentModel,   // ← uses dynamic model
        max_tokens: 8000,
        tools,
        tool_choice: "auto",
        messages,
        stream: true,
      });

      let fullContent = "";
      let toolCalls = {};

      for await (const chunk of stream) {
        const delta = chunk.choices[0]?.delta;
        const finishReason = chunk.choices[0]?.finish_reason;

        if (delta?.content) {
          fullContent += delta.content;
          send({ type: "text", content: delta.content });
        }

        if (delta?.tool_calls) {
          for (const tc of delta.tool_calls) {
            const idx = tc.index;
            if (!toolCalls[idx]) toolCalls[idx] = { id: "", name: "", args: "" };
            if (tc.id) toolCalls[idx].id = tc.id;
            if (tc.function?.name) toolCalls[idx].name += tc.function.name;
            if (tc.function?.arguments) toolCalls[idx].args += tc.function.arguments;
          }
        }

        if (finishReason === "tool_calls") {
          messages.push({
            role: "assistant",
            content: fullContent || null,
            tool_calls: Object.values(toolCalls).map(tc => ({
              id: tc.id, type: "function",
              function: { name: tc.name, arguments: tc.args },
            })),
          });

          for (const tc of Object.values(toolCalls)) {
            send({ type: "tool_call", name: tc.name, args: JSON.parse(tc.args) });
            const result = await mcpClient.callTool(tc.name, JSON.parse(tc.args));
            const content = result.content.filter(c => c.type === "text").map(c => c.text);
            send({ type: "tool_result", name: tc.name, result: content });
            messages.push({ role: "tool", tool_call_id: tc.id, content: JSON.stringify(content) });
          }

          toolCalls = {};
          break;
        }

        if (finishReason === "stop") {
          messages.push({ role: "assistant", content: fullContent });
          send({ type: "done" });
          res.end();
          return;
        }
      }
    }
  } catch (err) {
    send({ type: "error", message: err.message });
    res.end();
  }
});

// ── Resource endpoints ────────────────────────────────────────────────────

app.get("/resources", async (req, res) => {
  try {
    const resources = await mcpClient.listResources();
    res.json(resources);
  } catch (err) {
    res.status(500).json({ error: err.message });
  }
});

app.get("/resource", async (req, res) => {
  try {
    const { uri } = req.query;
    const contents = await mcpClient.readResource(uri);
    res.json(contents);
  } catch (err) {
    res.status(500).json({ error: err.message });
  }
});

app.listen(3000, () => console.log("🌐 Chat UI at http://localhost:3000"));