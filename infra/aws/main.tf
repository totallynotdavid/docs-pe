provider "aws" {
  region = var.region
}

# vps-setup's own tests/e2e/aws module hardcodes a 16 GB root disk, tags nothing
# beyond purpose and run_id, and names its security group from run_id alone, so a
# second server in one run would collide.
module "core" {
  source = "./server"

  role               = "core"
  run_id             = var.run_id
  instance_type      = var.core_instance_type
  root_volume_gb     = var.root_volume_gb
  ubuntu_version     = var.ubuntu_version
  ssh_source_cidr    = var.ssh_source_cidr
  user_data_template = "${var.vps_setup_dir}/tests/e2e/aws/user-data.yaml.tftpl"
}

module "worker" {
  source = "./server"

  role               = "worker"
  run_id             = var.run_id
  instance_type      = var.worker_instance_type
  root_volume_gb     = var.root_volume_gb
  ubuntu_version     = var.ubuntu_version
  ssh_source_cidr    = var.ssh_source_cidr
  user_data_template = "${var.vps_setup_dir}/tests/e2e/aws/user-data.yaml.tftpl"
}
