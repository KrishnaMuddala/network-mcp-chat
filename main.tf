terraform {
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }
}

provider "aws" {
  region = "ap-southeast-1"  # Singapore - closest to you
}

# ── Networking ──────────────────────────────────────────────────────────
resource "aws_security_group" "netops_sg" {
  name        = "netops-chat-sg"
  description = "NetOps Chat - restricted access"

  ingress {
    description = "HTTPS from office/VPN only"
    from_port   = 443
    to_port     = 443
    protocol    = "tcp"
    cidr_blocks = [var.allowed_cidr]
  }

  ingress {
    description = "SSH admin"
    from_port   = 22
    to_port     = 22
    protocol    = "tcp"
    cidr_blocks = [var.allowed_cidr]
  }

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = { Name = "netops-chat-sg" }
}

# ── EC2 Instance (GPU) ────────────────────────────────────────────────────
resource "aws_instance" "netops_gpu" {
  ami                    = var.ubuntu_ami  # Ubuntu 22.04 in ap-southeast-1
  instance_type          = "c6i.2xlarge"
  key_name               = var.key_pair_name
  vpc_security_group_ids = [aws_security_group.netops_sg.id]

  root_block_device {
    volume_size = 100   # GB - model weights + Docker images
    volume_type = "gp3"
  }

  tags = { Name = "netops-chat-gpu" }

  user_data = <<-EOF
    #!/bin/bash
    apt update && apt install -y nvidia-driver-535 docker.io docker-compose-plugin
    curl -fsSL https://ollama.com/install.sh | sh
    systemctl enable ollama
    systemctl start ollama
    ollama pull qwen2.5:7b
    # 4. Deploy app
    git clone https://github.com/KrishnaMuddala/network-mcp-chat.git
    cd network-mcp-chat
    cp env.example .env && nano .env
    mkdir certs
    openssl req -x509 -nodes -days 365 -newkey rsa:2048 -keyout certs/privkey.pem -out certs/fullchain.pem -subj "/CN=netops-internal"
    docker compose -f docker-compose.prod.yml up --build -d
  EOF
}

# ── Elastic IP (stable address) ───────────────────────────────────────────
resource "aws_eip" "netops_eip" {
  instance = aws_instance.netops_gpu.id
  domain   = "vpc"
}

output "public_ip" {
  value = aws_eip.netops_eip.public_ip
}