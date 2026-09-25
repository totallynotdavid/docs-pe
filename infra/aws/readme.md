# AWS test servers

Two disposable Ubuntu servers on AWS EC2, `core` and `worker`, for a whole-portal
test. Each starts the way a Contabo server does: root login by password over
SSH, set by cloud-init. [vps-setup](https://github.com/totallynotdavid/vps-setup)
then installs Tailscale and hardening and closes public SSH, and everything after
that runs over the tailnet as the admin user. `core` gets Docker and Dokploy,
`worker` gets Docker. Deploying docs-pe onto them is not part of this page.

`scripts/e2e-aws.sh` drives it. Its `vps_setup_ref` is the only pin of vps-setup:
`bin/provision`, the cloud-init template Terraform reads and the sweep all come
from that commit, in a checkout under `.data/vps-setup`.

## Prerequisites

- An IAM user with vps-setup's `tests/e2e/aws/iam-policy.json` attached: `ec2:*`
  in `us-east-1` and `ssm:GetParameter` on Canonical's Ubuntu AMI parameters.
  Nothing else is used. Pass its credentials with `AWS_PROFILE`, or
  `AWS_ACCESS_KEY_ID` and `AWS_SECRET_ACCESS_KEY`.
- `TS_TEST_KEY_FILE`: a file holding an OAuth client secret for `tag:vps-test`
  (scope `auth_keys`), or a reusable, ephemeral, pre-approved auth key with that
  tag. Set `TS_TAGS` if yours carries other tags. This machine must be on the
  tailnet, and the [tailnet policy](https://github.com/totallynotdavid/vps-setup/blob/master/docs/tailnet-policy.md)
  must let it SSH to that tag as the admin user.
- OpenSSH 8.4 or later, for `SSH_ASKPASS_REQUIRE`, and `git` and `curl`.
  `mise install` provides `terraform` and `aws`.

The script checks every input before its first AWS call and names all the missing
ones together, with status 2.

## Commands

| Command | Does |
| --- | --- |
| `mise run e2e:aws` | `up`, `provision`, then `$E2E_AWS_HOOK`, and always `down` on exit |
| `mise run e2e:aws:up` | creates both servers, waits for root login and cloud-init, prints the public IPs |
| `mise run e2e:aws:provision` | runs `bin/provision` on each server, then installs Docker and Dokploy |
| `mise run e2e:aws:down` | destroys both servers. Safe to repeat |
| `mise run e2e:aws:sweep` | terminates leftovers tagged `purpose=vps-setup-e2e` older than two hours, by vps-setup's own sweep |

`scripts/e2e-aws.sh up|provision|test|down|sweep|all` is the same interface
without mise. `test` runs the executable in `E2E_AWS_HOOK` with `CORE_HOST` and
`WORKER_HOST` set to the tailnet names and `ADMIN_USER` to the admin account. It
is where a deployment test goes, and does nothing when the variable is unset.

`UBUNTU_VERSION` picks 24.04 or 26.04 (default 26.04). On 26.04 Docker's
repository has no 28.5.0, which Dokploy's installer pins, so `provision`
installs Docker first there and the installer skips its own step. On 24.04 the
installer brings its own Docker on `core`. `TF_VAR_core_instance_type` and
`TF_VAR_worker_instance_type` set each server's type, and
`TF_VAR_root_volume_gb` the disk of both. The defaults are `m7i-flex.large`
(8 GB) for `core`, which runs Dokploy and the portal core and does not fit in
2 GB, `t3.small` for `worker`, and 30 GB, the disk Dokploy's installer asks
for.

## What is created

Per server, in `us-east-1`: one EC2 instance from Canonical's current Ubuntu AMI
with IMDSv2 required, one security group open on tcp/22 to this machine's public
IP as a `/32`, and the root volume. Everything carries `purpose=vps-setup-e2e`,
`project=docs-pe`, `role=core|worker` and `run_id`; the security group also
carries `created_at`, which the sweep needs. The generated root passwords live in
Terraform state under `infra/aws/`, which is gitignored, and reach SSH through
`SSH_ASKPASS` only. There is no remote backend.

`infra/aws/server` is one server. vps-setup's `tests/e2e/aws` module could not be
used as it is: it fixes the root disk at 16 GB, cannot take extra tags, and names
its security group from `run_id` alone. Only its cloud-init template is read from
the pinned checkout.

## Confirm everything is destroyed

After `down`, each of these prints nothing:

```sh
aws ec2 describe-instances --region us-east-1 \
  --filters Name=tag:project,Values=docs-pe \
    Name=instance-state-name,Values=pending,running,stopping,stopped \
  --query 'Reservations[].Instances[].InstanceId' --output text
aws ec2 describe-security-groups --region us-east-1 \
  --filters Name=tag:project,Values=docs-pe \
  --query 'SecurityGroups[].GroupId' --output text
aws ec2 describe-volumes --region us-east-1 \
  --filters Name=tag:project,Values=docs-pe \
  --query 'Volumes[].VolumeId' --output text
```

The test nodes are ephemeral, so `docs-pe-e2e-*` leaves the tailnet on its own
some time after the servers stop. If a run crashed and the state is gone, run
`mise run e2e:aws:sweep`. Check the AWS console for leftovers after any
interrupted run: an instance keeps costing until it is terminated.

## Change the vps-setup pin

Edit `vps_setup_ref` in `scripts/e2e-aws.sh`. The next run replaces the checkout.
