variable "allowed_cidr" {
  type        = string
  description = "Your IP for SSH access e.g. 1.2.3.4/32"
}

variable "key_pair_name" {
  type        = string
  description = "Existing EC2 key pair name"
}

variable "ubuntu_ami" {
  type        = string
  description = "Ubuntu 22.04 AMI for ap-southeast-1"
  default     = "ami-0fa377108253bf620"
}