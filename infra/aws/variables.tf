variable "region" {
  description = "AWS region for every resource."
  type        = string
  default     = "us-east-1"
}

variable "instance_type" {
  description = "EC2 instance type of both servers. Dokploy needs 2 GB of RAM."
  type        = string
  default     = "t3.small"
}

variable "root_volume_gb" {
  description = "Root disk of both servers in GB. Dokploy's installer asks for 30."
  type        = number
  default     = 30

  validation {
    condition     = var.root_volume_gb >= 30
    error_message = "root_volume_gb must be at least 30, the disk Dokploy's installer asks for."
  }
}

variable "ubuntu_version" {
  description = "Ubuntu release, as it appears in Canonical's SSM parameter path."
  type        = string
  default     = "26.04"

  validation {
    condition     = contains(["24.04", "26.04"], var.ubuntu_version)
    error_message = "ubuntu_version must be 24.04 or 26.04, the releases vps-setup's Dokploy recipe was run on."
  }
}

variable "ssh_source_cidr" {
  description = "The only source allowed to reach tcp/22: the operator's public IPv4 address as a /32."
  type        = string

  validation {
    condition     = can(regex("^([0-9]{1,3}\\.){3}[0-9]{1,3}/32$", var.ssh_source_cidr)) && can(cidrhost(var.ssh_source_cidr, 0))
    error_message = "ssh_source_cidr must be a single IPv4 address as a /32."
  }
}

variable "run_id" {
  description = "Identifies one run; it is tagged on every resource and forms the Tailscale node names."
  type        = string

  validation {
    condition     = can(regex("^[a-z0-9]([a-z0-9-]{0,28}[a-z0-9])?$", var.run_id))
    error_message = "run_id must be lowercase letters, digits and hyphens, 1-30 characters, starting and ending with a letter or digit."
  }
}

variable "vps_setup_dir" {
  description = "Checkout of vps-setup; its tests/e2e/aws/user-data.yaml.tftpl gives each server its start state."
  type        = string
}
