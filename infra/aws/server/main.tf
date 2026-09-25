terraform {
  required_providers {
    aws = {
      source = "hashicorp/aws"
    }
    random = {
      source = "hashicorp/random"
    }
  }
}

locals {
  # purpose is the tag vps-setup's `aws.sh sweep` reaps by.
  tags = {
    purpose = "vps-setup-e2e"
    project = "docs-pe"
    role    = var.role
    run_id  = var.run_id
  }
}

# Both supported releases are published under ebs-gp3.
data "aws_ssm_parameter" "ubuntu_ami" {
  name = "/aws/service/canonical/ubuntu/server/${var.ubuntu_version}/stable/current/amd64/hvm/ebs-gp3/ami-id"
}

data "aws_vpc" "default" {
  default = true
}

resource "random_password" "root" {
  length  = 24
  special = false
}

resource "aws_security_group" "ssh" {
  name        = "docs-pe-e2e-${var.run_id}-${var.role}"
  description = "docs-pe end-to-end run ${var.run_id} ${var.role}: SSH from the operator only"
  vpc_id      = data.aws_vpc.default.id

  ingress {
    description = "SSH from the operator"
    from_port   = 22
    to_port     = 22
    protocol    = "tcp"
    cidr_blocks = [var.ssh_source_cidr]
  }

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  # A security group has no creation time in the EC2 API, and vps-setup's sweep needs one.
  tags = merge(local.tags, { created_at = timestamp() })

  lifecycle {
    ignore_changes = [tags["created_at"], tags_all["created_at"]]
  }
}

resource "aws_instance" "server" {
  ami                         = data.aws_ssm_parameter.ubuntu_ami.insecure_value
  instance_type               = var.instance_type
  vpc_security_group_ids      = [aws_security_group.ssh.id]
  associate_public_ip_address = true

  user_data = templatefile(var.user_data_template, {
    root_password = random_password.root.result
  })
  user_data_replace_on_change = true

  metadata_options {
    http_tokens = "required"
  }

  root_block_device {
    volume_type = "gp3"
    volume_size = var.root_volume_gb
  }

  tags        = local.tags
  volume_tags = local.tags
}
