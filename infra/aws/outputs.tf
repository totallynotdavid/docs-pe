output "run_id" {
  value = var.run_id
}

output "core_public_ip" {
  value = module.core.public_ip
}

output "worker_public_ip" {
  value = module.worker.public_ip
}

output "core_root_password" {
  value     = module.core.root_password
  sensitive = true
}

output "worker_root_password" {
  value     = module.worker.root_password
  sensitive = true
}
