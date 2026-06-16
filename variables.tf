variable "allowed_cidr" {
  description = "Your office/VPN IP range, e.g. 1.2.3.4/32"
  type        = string
}

variable "key_pair_name" {
  description = "Existing EC2 key pair name"
  type        = string
}

variable "ubuntu_ami" {
  description = "Ubuntu 22.04 AMI ID for ap-southeast-1"
  type        = string
  default     = "ami-0fa377108253bf620"  # verify current AMI ID
}