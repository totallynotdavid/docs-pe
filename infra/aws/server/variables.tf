variable "role" {
  description = "core or worker; tagged on every resource."
  type        = string

  validation {
    condition     = contains(["core", "worker"], var.role)
    error_message = "role must be core or worker."
  }
}

variable "run_id" {
  description = "Identifies one run; tagged on every resource."
  type        = string
}

variable "instance_type" {
  description = "EC2 instance type."
  type        = string
}

variable "root_volume_gb" {
  description = "Root disk in GB."
  type        = number
}

variable "ubuntu_version" {
  description = "Ubuntu release, as it appears in Canonical's SSM parameter path."
  type        = string
}

variable "ssh_source_cidr" {
  description = "The only source allowed to reach tcp/22."
  type        = string
}

variable "user_data_template" {
  description = "cloud-init template that sets the root password and enables root login over SSH."
  type        = string
}
