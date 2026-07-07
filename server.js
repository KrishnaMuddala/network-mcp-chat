import express from "express";
import { Client } from "@modelcontextprotocol/sdk/client/index.js";
import { StreamableHTTPClientTransport } from "@modelcontextprotocol/sdk/client/streamableHttp.js";
import OpenAI from "openai";
import * as dotenv from "dotenv";
import multer from 'multer';
import pdfParse from 'pdf-parse/lib/pdf-parse.js';
import mammoth from 'mammoth';
import fs from 'fs';
if (!fs.existsSync('uploads')) fs.mkdirSync('uploads');
import session from 'express-session';
import bcrypt from 'bcryptjs';
import { readFileSync } from 'fs';
const users = JSON.parse(readFileSync('./users.json', 'utf-8'));
import path from 'path';
import { dirname } from 'path';
import { fileURLToPath } from 'url';
const __filename = fileURLToPath(import.meta.url);
const __dirname = dirname(__filename);

dotenv.config();

const app = express();
app.use(express.json());


app.use(session({
  secret: process.env.SESSION_SECRET || 'change-this-to-a-random-string',
  resave: false,
  saveUninitialized: false,
  cookie: {
    secure: false,        // set true when behind HTTPS/nginx
    maxAge: 8 * 60 * 60 * 1000  // 8 hour  session
  }
}));

// ── Auth middleware ───────────────────────────────────────────────────────
function requireAuth(req, res, next) {
  if (req.session?.user) return next();
  if (req.path === '/login' || req.path === '/login.html' || req.path.startsWith('/css') || req.path.startsWith('/js')) {
    return next();
  }
  return res.redirect('/login.html');
}

// ── Login route ──────────────────────────────────────────────────────────
app.post('/login', express.urlencoded({ extended: true }), (req, res) => {
  const { username, password } = req.body;
  const user = users.find(u => u.username === username);

  if (user && bcrypt.compareSync(password, user.passwordHash)) {
    req.session.user = username;
    return res.redirect('/');
  }
  return res.redirect('/login.html?error=1');
});


app.get("/graph-test", (req, res) => {
  res.sendFile(path.join(__dirname, "graph_test.html"));
});


app.post('/logout', (req, res) => {
  req.session.destroy(() => res.redirect('/login.html'));
});

// Apply auth to everything AFTER this line
app.use(requireAuth);

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

const forwardClient = new MCPClient(process.env.FORWARD_MCP_URL ?? "http://localhost:8000/mcp");
const ciscoClient = new MCPClient(
  process.env.CISCO_MCP_URL ?? "http://localhost:8001/mcp"
);
const memoryClient = new MCPClient(
  process.env.MEMORY_MCP_URL ?? "http://localhost:8002/mcp"
);
const sequentialClient = new MCPClient(
  process.env.SEQUENTIAL_MCP_URL ?? "http://localhost:8003/mcp"
);
await forwardClient.connect();
await ciscoClient.connect();
await memoryClient.connect();
await sequentialClient.connect();

const forwardTools = await forwardClient.listTools();
const ciscoTools   = await ciscoClient.listTools();
const memoryTools  = await memoryClient.listTools();
const sequentialTools = await sequentialClient.listTools();
const mcpTools = [...forwardTools, ...ciscoTools, ...memoryTools, ...sequentialTools];

// ── Tool name → client lookup map ───────────────────────────────────────
// This fixes "Unknown tool" errors — without this map, ALL tool calls
// were being sent to forwardClient even when the tool belongs to ciscoClient.
const toolClientMap = {};
for (const t of forwardTools) toolClientMap[t.name] = forwardClient;
for (const t of ciscoTools)   toolClientMap[t.name] = ciscoClient;
for (const t of memoryTools)  toolClientMap[t.name] = memoryClient;
for (const t of sequentialTools) toolClientMap[t.name] = sequentialClient;
function getClientForTool(toolName) {
  const client = toolClientMap[toolName];
  if (!client) {
    throw new Error(`No MCP client registered for tool: ${toolName}`);
  }
  return client;
}

console.log(`✅ Connected to MCP servers with tools: ${mcpTools.map(t => t.name).join(", ")}`);
console.log(`   Forward Networks tools: ${forwardTools.map(t => t.name).join(", ")}`);
console.log(`   Cisco tools: ${ciscoTools.map(t => t.name).join(", ")}`);
console.log(`   Memory tools: ${memoryTools.map(t => t.name).join(", ")}`);
console.log(`   Sequential tools: ${sequentialTools.map(t => t.name).join(", ")}`);
const LLM_BASE_URL = process.env.LOCAL_LLM_BASE_URL ?? "http://localhost:11434/v1";

const openai = new OpenAI({
  baseURL: LLM_BASE_URL,
  apiKey: "ollama",
});

// ── Dynamic model selection ───────────────────────────────────────────────
let currentModel = process.env.LOCAL_LLM_MODEL ?? "qwen2.5:7b";

console.log("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━");
console.log("🤖 LLM Model     :", currentModel);
console.log("🌐 LLM Base URL  :", LLM_BASE_URL);
console.log("🔌 Forward MCP   :", process.env.FORWARD_MCP_URL ?? "http://localhost:8000/mcp");
console.log("🔌 Cisco MCP     :", process.env.CISCO_MCP_URL ?? "http://localhost:8001/mcp");
console.log("🔌 Memory MCP    :", process.env.MEMORY_MCP_URL ?? "http://localhost:8002/mcp");
console.log("🔌 Sequential MCP:", process.env.SEQUENTIAL_MCP_URL ?? "http://localhost:8003/mcp");
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

const SYSTEM_PROMPT = `You are a network operations assistant for Unicomm infrastructure.
You have access to four tool groups. Use EXACT tool names — never modify them.

════════════════════════════════════════
TOOL GROUP 1: MEMORY (use first and last)
════════════════════════════════════════
Use to remember context across conversations about network devices and topology.

When to use:
- START of every conversation → search memory for relevant context about the user's question
- END of every conversation  → save any new network facts discovered

What to save to memory:
- Device facts: IP address, hostname, model, OS version, role (core/access/edge)
- Topology facts: which devices connect to which, VLAN assignments, uplinks
- Issue history: past failures, workarounds, known problems
- Path facts: known good/bad paths between endpoints

How to use:
1. At start → search_memory(query) for relevant entities
2. During chat → note any new network facts
3. At end → create_entity() and add_observation() for new facts discovered

════════════════════════════════════════
TOOL GROUP 2: SEQUENTIAL THINKING
════════════════════════════════════════
Use for: complex multi-step problems, troubleshooting, root cause analysis.

When to use:
- User asks "why is X not working"
- User asks for a plan or step-by-step analysis
- Problem requires more than 2 tool calls to solve
- You need to reason through trade-offs

How to use:
- Call sequential_thinking BEFORE calling data tools for complex problems
- Break the problem into clear steps, then execute each step

════════════════════════════════════════
TOOL GROUP 3: FORWARD NETWORKS
════════════════════════════════════════
Use for: network topology, device inventory, hardware EOL, path tracing, compliance.

Rules:
1. ALWAYS call list_networks FIRST to get a valid network_id — never guess one
2. Inventory        → get_device_basic_info(network_id, output_format)
3. Hardware EOL     → get_hardware_support(network_id, output_format)
4. Path tracing     → search_paths(network_id, dst_ip, src_ip, output_format)
5. Graph output     → add output_format='graph' to the tool above
   CORRECT:   get_device_basic_info(network_id='123', output_format='graph')
   INCORRECT: generate_graph('describe inventory') ← never pass text descriptions

════════════════════════════════════════
TOOL GROUP 4: CISCO SWITCH
════════════════════════════════════════
Use for: interfaces, VLANs, MAC tables, ARP, spanning tree, switch config.

Rules:
1. Call cisco_list_commands() first if unsure which command to use
2. All queries → cisco_show(host, command)
3. Only read-only show commands are allowed
4. Before running a command, check memory for known facts about this host

════════════════════════════════════════
EXECUTION ORDER (follow this every time)
════════════════════════════════════════
1. SEARCH MEMORY    → find any saved context about devices/networks in the question
2. THINK IF COMPLEX → use sequential_thinking for multi-step or troubleshooting tasks
3. VALIDATE INPUTS  → call list_networks or cisco_list_commands to confirm IDs/hosts
4. EXECUTE TOOLS    → call the correct data tool with validated parameters
5. EXPLAIN RESULTS  → summarize findings in plain English, flag issues
6. SAVE TO MEMORY   → store any new network facts discovered this session

════════════════════════════════════════
GENERAL RULES
════════════════════════════════════════
- Use EXACT tool names — case-sensitive, never abbreviated
- Explain results in plain English — concise, relevant, no jargon padding
- Generate graphs only from real JSON data — never from text descriptions
- If a tool fails → explain why and suggest alternatives
- Multiple tool groups CAN be used in one response (e.g. memory + cisco + forward networks)
- When asked to analyze, check, or review results already shown — DO NOT describe what you will do, just do it immediately
- If the last tool result is already in context, analyze it directly without calling the tool again
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

  const tools = mcpTools.map(t => ({
    type: "function",
    function: { name: t.name, description: t.description, parameters: t.inputSchema },
  }));

  try {
    while (true) {
      const stream = await openai.chat.completions.create({
        model: currentModel,
        max_tokens: 8000,
        tools,
        tool_choice: "auto",
        messages,
        stream: true,
        parallel_tool_calls: false,
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

  const client = getClientForTool(tc.name);
  const result = await client.callTool(tc.name, JSON.parse(tc.args));
  const rawContent = result.content.filter(c => c.type === "text").map(c => c.text);

  // ── Clean up escaped JSON for display ──────────────────────────
  const displayContent = rawContent.map(text => {
    try {
      const parsed = JSON.parse(text);
      // If it has an output field, show just the output cleanly
      if (parsed.output) return parsed.output;
      return JSON.stringify(parsed, null, 2);
    } catch {
      return text;
    }
  });

  send({ type: "tool_result", name: tc.name, result: displayContent });
  // Pass full JSON to LLM for analysis, clean text to browser
  messages.push({ role: "tool", tool_call_id: tc.id, content: JSON.stringify(rawContent) });
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
    const promptMessages = await forwardClient.client.getPrompt({
      name: command,
      arguments: { doc_id: docId },
    });

    for (const pm of promptMessages.messages) {
      messages.push({
        role: pm.role,
        content: typeof pm.content === "object" ? pm.content.text : pm.content,
      });
    }

    // ── FIXED: reuse global mcpTools (has both servers) instead of
    // refetching from forwardClient only ──
    const tools = mcpTools.map(t => ({
      type: "function",
      function: { name: t.name, description: t.description, parameters: t.inputSchema },
    }));

    while (true) {
      const stream = await openai.chat.completions.create({
        model: currentModel,
        max_tokens: 8000,
        tools,
        tool_choice: "auto",
        messages,
        stream: true,
        parallel_tool_calls: false,
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

            try {
              // ── FIXED: route to correct MCP client based on tool name ──
              const client = getClientForTool(tc.name);
              const result = await client.callTool(tc.name, JSON.parse(tc.args));
              const content = result.content.filter(c => c.type === "text").map(c => c.text);
              send({ type: "tool_result", name: tc.name, result: content });
              messages.push({ role: "tool", tool_call_id: tc.id, content: JSON.stringify(content) });
            } catch (toolErr) {
              const errMsg = `Tool error: ${toolErr.message}`;
              send({ type: "tool_result", name: tc.name, result: [errMsg] });
              messages.push({ role: "tool", tool_call_id: tc.id, content: errMsg });
            }
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
    const resources = await forwardClient.listResources();
    res.json(resources);
  } catch (err) {
    res.status(500).json({ error: err.message });
  }
});

app.get("/resource", async (req, res) => {
  try {
    const { uri } = req.query;
    const contents = await forwardClient.readResource(uri);
    res.json(contents);
  } catch (err) {
    res.status(500).json({ error: err.message });
  }
});



const upload = multer({ dest: 'uploads/', limits: { fileSize: 20 * 1024 * 1024 } }); // 20MB cap

// --- Text extraction per file type ---
async function extractText(filePath, mimetype, originalName) {
  const ext = originalName.toLowerCase().split('.').pop();

  if (ext === 'pdf') {
    const buf = fs.readFileSync(filePath);
    const data = await pdfParse(buf);
    return data.text;
  }

  if (ext === 'docx') {
    const result = await mammoth.extractRawText({ path: filePath });
    return result.value;
  }

  if (ext === 'txt' || ext === 'csv' || ext === 'log') {
    return fs.readFileSync(filePath, 'utf-8');
  }

  throw new Error(`Unsupported file type: ${ext}`);
}

// --- Chunking (fits qwen2.5:7b context) ---
function chunkText(text, maxChars = 3000) {
  const chunks = [];
  for (let i = 0; i < text.length; i += maxChars) {
    chunks.push(text.slice(i, i + maxChars));
  }
  return chunks;
}
const LOCAL_LLM_CHAT_URL = process.env.LOCAL_LLM_CHAT_URL ?? 'http://host.docker.internal:11434/api/chat';
// --- Call Ollama ---
async function ollamaChat(prompt, timeoutMs = 300000) {
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), timeoutMs);
  try {
    const res = await fetch(LOCAL_LLM_CHAT_URL, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        model: currentModel,
        messages: [{ role: 'user', content: prompt }],
        stream: false
      }),
      signal: controller.signal
    });
    const data = await res.json();
    return data.message.content;
  } finally {
    clearTimeout(timeout);
  }
}

// --- Map-reduce summarization ---
async function summarizeDocument(text) {
  const chunks = chunkText(text);

  if (chunks.length === 1) {
    return await ollamaChat(`Summarize the following document concisely:\n\n${chunks[0]}`);
  }

  // Map: summarize each chunk
  const chunkSummaries = [];
  for (const [idx, chunk] of chunks.entries()) {
    console.log(`Summarizing chunk ${idx + 1}/${chunks.length}`);
    const summary = await ollamaChat(
      `Summarize this section concisely, keeping key facts and figures:\n\n${chunk}`
    );
    chunkSummaries.push(summary);
  }

  // Reduce: combine chunk summaries
  const combined = chunkSummaries.join('\n\n');
  return await ollamaChat(
    `Combine these section summaries into one coherent overall summary:\n\n${combined}`
  );
}

// --- Route ---
app.post('/api/upload-summarize', upload.single('file'), async (req, res) => {
  try {
    const { path: filePath, mimetype, originalname } = req.file;
    const text = await extractText(filePath, mimetype, originalname);

    if (!text || text.trim().length === 0) {
      return res.status(400).json({ error: 'No text could be extracted from this file.' });
    }

    const summary = await summarizeDocument(text);

    fs.unlinkSync(filePath); // cleanup temp file
    res.json({ filename: originalname, summary, char_count: text.length });
  } catch (err) {
    console.error(err);
    res.status(500).json({ error: err.message });
  }
});

app.listen(3000, () => console.log("🌐 Chat UI at http://localhost:3000"));