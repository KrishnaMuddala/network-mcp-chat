terraform {
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }
}

provider "aws" {
  region = "ap-southeast-1"
}

# ── IAM Role for Secrets Manager access ──────────────────────────────────
resource "aws_iam_role" "netops_role" {
  name = "netops-chat-role"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Action    = "sts:AssumeRole"
      Effect    = "Allow"
      Principal = { Service = "ec2.amazonaws.com" }
    }]
  })
}

resource "aws_iam_role_policy" "netops_secrets_policy" {
  name   = "netops-secrets-access"
  role   = aws_iam_role.netops_role.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = ["secretsmanager:GetSecretValue"]
      Resource = "arn:aws:secretsmanager:ap-southeast-1:*:secret:netops-chat/*"
    }]
  })
}

resource "aws_iam_instance_profile" "netops_profile" {
  name = "netops-chat-profile"
  role = aws_iam_role.netops_role.name
}

# ── Security Group ────────────────────────────────────────────────────────
resource "aws_security_group" "netops_sg" {
  name        = "netops-chat-sg"
  description = "NetOps Chat - restricted access"

  ingress {
    description = "HTTPS - open to all (nginx handles auth)"
    from_port   = 443
    to_port     = 443
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }

  ingress {
    description = "SSH admin - your IP only"
    from_port   = 22
    to_port     = 22
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }

  ingress {
    description = "ICMP ping"
    from_port   = -1
    to_port     = -1
    protocol    = "icmp"
    cidr_blocks = ["0.0.0.0/0"]
  }

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = { Name = "netops-chat-sg" }
}

# ── EC2 Instance ──────────────────────────────────────────────────────────
resource "aws_instance" "netops_gpu" {
  ami                    = var.ubuntu_ami
  instance_type          = "g4dn.2xlarge"   # swap to g4dn.xlarge or c6i.2xlarge after GPU quota approved
  key_name               = var.key_pair_name
  vpc_security_group_ids = [aws_security_group.netops_sg.id]
  iam_instance_profile   = aws_iam_instance_profile.netops_profile.name

  root_block_device {
    volume_size = 100
    volume_type = "gp3"
  }

  tags = { Name = "netops-chat-cpu" }

  user_data = <<-EOF
#!/bin/bash
set -e
exec > /var/log/cloud-init-output.log 2>&1

echo "=== [1/10] System update ==="
apt update && apt upgrade -y
apt install -y git curl awscli ca-certificates gnupg

# Install Docker from official Docker repo (not Ubuntu's)
install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/ubuntu/gpg | gpg --dearmor -o /etc/apt/keyrings/docker.gpg
chmod a+r /etc/apt/keyrings/docker.gpg

echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg]  https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo "$VERSION_CODENAME") stable" | tee /etc/apt/sources.list.d/docker.list > /dev/null

apt update
apt install -y docker-ce docker-ce-cli containerd.io docker-compose-plugin

systemctl enable docker
systemctl start docker

echo "=== [2/10] Install Ollama ==="
curl -fsSL https://ollama.com/install.sh | sh

echo "=== [3/10] Start Ollama + pull model ==="
export HOME=/root
export OLLAMA_HOST=0.0.0.0:11434
sudo nohup ollama serve > /var/log/ollama.log 2>&1 &
sleep 20

# Wait until Ollama API responds
until curl -s http://localhost:11434/api/tags > /dev/null; do
echo "Waiting for Ollama..."
sleep 5
done

HOME=/root ollama pull qwen2.5:7b
echo "Model pulled successfully"

echo "=== [4/10] Clone network-mcp-chat repo ==="
cd /home/ubuntu
git clone https://github.com/KrishnaMuddala/network-mcp-chat.git
cd network-mcp-chat

echo "=== [5/10] Pull secrets ==="
aws secretsmanager get-secret-value \
--secret-id "netops-chat/env" \
--region ap-southeast-1 \
--query SecretString \
--output text > .env

aws secretsmanager get-secret-value \
--secret-id "netops-chat/users" \
--region ap-southeast-1 \
--query SecretString \
--output text > users.json

echo "=== [6/10] TLS cert ==="
mkdir -p certs
openssl req -x509 -nodes -days 365 -newkey rsa:2048 \
-keyout certs/privkey.pem \
-out certs/fullchain.pem \
-subj "/CN=netops-internal"

echo "=== [7/10] Deploy network-mcp-chat ==="
chown -R ubuntu:ubuntu /home/ubuntu/network-mcp-chat
docker compose -f docker-compose.prod.yml up --build -d
echo "=== [8/11] Clone server repo ==="
cd /home/ubuntu
git clone https://github.com/KrishnaMuddala/server.git
cd server/src/memory

echo "=== [8/10] Pull secrets ==="
aws secretsmanager get-secret-value \
--secret-id "netops-chat/env" \
--region ap-southeast-1 \
--query SecretString \
--output text > .env

echo "=== [9/10] Deploy server tool ==="
chown -R ubuntu:ubuntu /home/ubuntu/server/src/memory
docker compose -f docker-compose.prod.yaml up --build -d
echo "=== [10/10] Ollam restarting server tool ==="
pkill -f ollama
export HOME=/root
export OLLAMA_HOST=0.0.0.0:11434

# Start the ollama service in the background and redirect logs
nohup ollama serve > /var/log/ollama.log 2>&1 &

# Wait for the service to start
sleep 5

# Verify the API endpoint
curl http://localhost:11434/api/tags
echo "=== Setup Complete ==="
EOF
}

# ── Elastic IP ────────────────────────────────────────────────────────────
resource "aws_eip" "netops_eip" {
  instance = aws_instance.netops_gpu.id
  domain   = "vpc"
}

output "ssh_command" {
  value = "ssh -i ec2-keypair.pem ubuntu@${aws_eip.netops_eip.public_ip}"
}

output "webui_url" {
  value = "https://${aws_eip.netops_eip.public_ip}"
}
