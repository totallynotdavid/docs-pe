output "public_ip" {
  value = aws_instance.server.public_ip
}

output "root_password" {
  value     = random_password.root.result
  sensitive = true
}
