#!/usr/bin/env bash
# Disposable test servers on AWS EC2 for a whole-portal test: a `core` and a
# `worker` that start the way a Contabo server does (root login with a password
# over SSH), are provisioned with vps-setup, get Docker and Dokploy, and are
# destroyed afterwards.
set -euo pipefail

repo=$(cd "$(dirname "$0")/.." && pwd)
self=$repo/scripts/e2e-aws.sh
tf_dir=$repo/infra/aws

# The one pin of vps-setup. bin/provision, known-hosts.sh, the first-boot
# cloud-init template that Terraform reads, and the sweep all come from this commit.
vps_setup_repo=https://github.com/totallynotdavid/vps-setup.git
vps_setup_ref=dfca409bd4f5d05c5b5f887b376074a9facea6e8
vps_dir=$repo/.data/vps-setup

# The IAM policy allows this region only.
region=us-east-1
ubuntu_version=${UBUNTU_VERSION:-26.04}
admin=${ADMIN_USER:-admin}
export ADMIN_USER=$admin
export AWS_REGION=$region
export TS_TAGS=${TS_TAGS:-tag:vps-test}
export TS_EPHEMERAL=1
export TF_VAR_region=$region
export TF_VAR_ubuntu_version=$ubuntu_version
export TF_VAR_vps_setup_dir=$vps_dir

roles=(core worker)

print_usage() {
	cat <<'EOF_USAGE'
usage: e2e-aws.sh up | provision | test | down | sweep | all

  up         create core and worker and wait until root and its password work; prints their public IPs
  provision  vps-setup's bin/provision on each server, then Docker on both and Dokploy on core
  test       run $E2E_AWS_HOOK against the provisioned servers, if set
  down       destroy both servers; safe to repeat
  sweep      terminate leftovers older than two hours, in case a run crashed
  all        up, provision, test, then down; always destroys

environment:
  AWS_PROFILE        or AWS_ACCESS_KEY_ID and AWS_SECRET_ACCESS_KEY; for every command
  TS_TEST_KEY_FILE   file holding an OAuth client secret or an auth key; for provision and all
  E2E_AWS_HOOK       executable that test runs; it gets CORE_HOST, WORKER_HOST and ADMIN_USER
  ADMIN_USER         admin account to create (default: admin)
  TS_TAGS            tags of the key or client (default: tag:vps-test)
  UBUNTU_VERSION     24.04 or 26.04 (default: 26.04)
  TF_VAR_core_instance_type     core's instance type (default: m7i-flex.large, 8 GB)
  TF_VAR_worker_instance_type   worker's instance type (default: t3.small)
  TF_VAR_root_volume_gb         disk of both servers in GB (default: 30)
EOF_USAGE
}

usage() {
	print_usage >&2
	exit 2
}

log() {
	printf '==> %s\n' "$*" >&2
}

die() {
	printf 'error: %s\n' "$*" >&2
	exit 1
}

tf() {
	terraform -chdir="$tf_dir" "$@"
}

# The checkout is a cache of the pinned commit, so anything else in it is replaced.
ensure_vps_setup() {
	[[ $(git -C "$vps_dir" rev-parse HEAD 2>/dev/null) == "$vps_setup_ref" ]] && return 0
	log "fetch vps-setup $vps_setup_ref"
	if [[ ! -d $vps_dir/.git ]]; then
		git init -q "$vps_dir"
		git -C "$vps_dir" remote add origin "$vps_setup_repo"
	fi
	git -C "$vps_dir" fetch -q --depth 1 origin "$vps_setup_ref"
	git -C "$vps_dir" checkout -q -f --detach FETCH_HEAD
}

# Every input is checked before the first AWS call, and all the missing ones are named together.
require_inputs() {
	local needs_key=$1 problems=() credentials_error tool
	for tool in terraform aws git curl ssh timeout; do
		command -v "$tool" >/dev/null || problems+=("$tool is not on PATH; run this through mise (mise run e2e:aws:...), which provides terraform and aws")
	done
	if [[ -z ${AWS_PROFILE:-} && (-z ${AWS_ACCESS_KEY_ID:-} || -z ${AWS_SECRET_ACCESS_KEY:-}) ]]; then
		problems+=("AWS credentials are not set; set AWS_PROFILE, or AWS_ACCESS_KEY_ID and AWS_SECRET_ACCESS_KEY")
	fi
	# Terraform ignores TF_VAR_ for an undeclared variable, so the old name would size nothing.
	if [[ -n ${TF_VAR_instance_type:-} ]]; then
		problems+=("TF_VAR_instance_type is no longer read; set TF_VAR_core_instance_type and TF_VAR_worker_instance_type")
	fi
	if [[ $ubuntu_version != 24.04 && $ubuntu_version != 26.04 ]]; then
		problems+=("UBUNTU_VERSION='$ubuntu_version' is not 24.04 or 26.04, the releases vps-setup's Dokploy recipe covers")
	fi
	if ((needs_key)); then
		if [[ -z ${TS_TEST_KEY_FILE:-} ]]; then
			problems+=("TS_TEST_KEY_FILE is not set; set it to a file holding an OAuth client secret, or a reusable, ephemeral auth key, for $TS_TAGS")
		elif [[ ! -f $TS_TEST_KEY_FILE || ! -r $TS_TEST_KEY_FILE ]]; then
			problems+=("TS_TEST_KEY_FILE='$TS_TEST_KEY_FILE' is not a readable file")
		fi
	fi
	if ((${#problems[@]})); then
		printf 'error: %s\n' "${problems[@]}" >&2
		exit 2
	fi
	ensure_vps_setup
	if ! credentials_error=$(aws sts get-caller-identity --region "$region" --output text 2>&1 >/dev/null); then
		printf 'error: no usable AWS credentials: aws sts get-caller-identity failed\n%s\n' "$credentials_error" >&2
		exit 2
	fi
}

operator_cidr() {
	local ip
	ip=$(curl -fsS --max-time 10 https://checkip.amazonaws.com | tr -d '[:space:]') ||
		die "could not learn this machine's public IP from checkip.amazonaws.com"
	[[ $ip =~ ^([0-9]{1,3}\.){3}[0-9]{1,3}$ ]] || die "checkip.amazonaws.com returned '$ip', not an IPv4 address"
	printf '%s/32\n' "$ip"
}

new_run_id() {
	od -An -N4 -tx1 /dev/urandom | tr -d ' \n'
}

server_output() {
	tf output -raw "$1" 2>/dev/null || die "there are no servers; run '$0 up' first"
}

node_name() {
	printf 'docs-pe-e2e-%s-%s\n' "$(server_output run_id)" "$1"
}

# The password reaches OpenSSH only through the environment of the command it starts.
# Callers may run this under || or if, where errexit is off, so failures exit explicitly.
with_root_password() (
	role=$1
	shift
	dir=$(mktemp -d) || exit 1
	trap 'rm -rf "$dir"' EXIT
	trap 'exit 130' INT
	trap 'exit 143' TERM
	cat >"$dir/askpass" <<'EOF_ASKPASS'
#!/bin/sh
printf '%s\n' "$E2E_ROOT_PASSWORD"
EOF_ASKPASS
	chmod 700 "$dir/askpass"
	E2E_ROOT_PASSWORD=$(server_output "${role}_root_password") || exit 1
	export E2E_ROOT_PASSWORD SSH_ASKPASS=$dir/askpass SSH_ASKPASS_REQUIRE=force
	"$@"
)

wait_until_ready() {
	local role=$1 ip=$2 deadline=$((SECONDS + 300)) output status
	log "wait for root and its password over SSH on $role, and for cloud-init"
	while ((SECONDS < deadline)); do
		status=0
		output=$(with_root_password "$role" timeout "$((deadline - SECONDS))" ssh -n \
			-o PubkeyAuthentication=no -o NumberOfPasswordPrompts=1 -o ConnectTimeout=5 \
			-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o LogLevel=ERROR \
			"root@$ip" 'cloud-init status --wait' 2>&1) || status=$?
		case $status in
		# 2 is cloud-init finished with recoverable errors; the login already worked.
		0 | 2)
			log "$role cloud-init: ${output//$'\n'/ }"
			return 0
			;;
		255) sleep 5 ;;
		124) break ;;
		*) die "cloud-init failed on $role $ip (exit $status): $output" ;;
		esac
	done
	die "root and its password did not work on $role $ip within five minutes${output:+; last output: $output}"
}

cmd_up() {
	local run_id cidr role ip
	cidr=$(operator_cidr)
	tf init -input=false >&2
	if [[ -n $(tf state list 2>/dev/null) ]]; then
		die "servers from an earlier run still exist; run '$0 down' first"
	fi
	run_id=$(new_run_id)
	log "create run $run_id, SSH open to $cidr only"
	tf apply -input=false -auto-approve -var "run_id=$run_id" -var "ssh_source_cidr=$cidr" >&2
	for role in "${roles[@]}"; do
		ip=$(server_output "${role}_public_ip")
		wait_until_ready "$role" "$ip"
		printf '%s %s\n' "$role" "$ip"
	done
}

tailnet_ssh() {
	local name=$1
	shift
	ssh -o BatchMode=yes -o ConnectTimeout=15 -o StrictHostKeyChecking=yes \
		-o "UserKnownHostsFile=$(e2e_known_hosts "$name")" "$admin@$name" "$@"
}

install_docker() {
	log "install Docker on $1"
	# unattended-upgrades can hold the apt locks when the install starts. retry on failure.
	tailnet_ssh "$1" 'sudo bash -s' <<'REMOTE'
set -euo pipefail
for _ in 1 2 3; do
	curl -fsSL https://get.docker.com | sh && exit 0
	sleep 30
done
exit 1
REMOTE
}

install_dokploy() {
	log "install Dokploy on $1"
	tailnet_ssh "$1" 'sudo bash -s' <<'REMOTE'
set -euo pipefail
addr=$(ip -4 route get 192.0.2.1 | awk '{for (i = 1; i < NF; i++) if ($i == "src") print $(i + 1)}')
[ -n "$addr" ] || { echo "could not read the server's private address" >&2; exit 1; }
installer=$(mktemp)
curl -sSL https://dokploy.com/install.sh -o "$installer"
ADVERTISE_ADDR=$addr sh "$installer"
rm -f "$installer"
for _ in $(seq 1 60); do
	curl -fsS -m 4 -o /dev/null http://127.0.0.1:3000/ && exit 0
	sleep 3
done
echo "the Dokploy dashboard did not answer on 127.0.0.1:3000 within three minutes" >&2
exit 1
REMOTE
}

cmd_provision() {
	local role ip name
	# shellcheck source=/dev/null
	source "$vps_dir/tests/e2e/known-hosts.sh"
	for role in "${roles[@]}"; do
		ip=$(server_output "${role}_public_ip")
		name=$(node_name "$role")
		log "provision $role as $name"
		with_root_password "$role" "$vps_dir/bin/provision" --key "$TS_TEST_KEY_FILE" "root@$ip" "$name"
	done
	# Dokploy's installer pins Docker 28.5.0, which Docker's repository for 26.04 does not carry.
	# on 26.04, Docker goes first and the installer skips its own step.
	if [[ $ubuntu_version != 24.04 ]]; then
		install_docker "$(node_name core)"
	fi
	install_dokploy "$(node_name core)"
	install_docker "$(node_name worker)"
}

cmd_test() {
	if [[ -z ${E2E_AWS_HOOK:-} ]]; then
		log "E2E_AWS_HOOK is not set; nothing to run against the servers"
		return 0
	fi
	CORE_HOST=$(node_name core) WORKER_HOST=$(node_name worker) "$E2E_AWS_HOOK"
}

# Destroying only reads the state, so the required variables take placeholders.
cmd_down() {
	log "destroy"
	tf init -input=false >&2
	tf destroy -input=false -auto-approve -var run_id=destroy -var ssh_source_cidr=192.0.2.1/32 >&2
}

# vps-setup's sweep matches on the purpose tag, which these servers carry.
cmd_sweep() {
	"$vps_dir/tests/e2e/aws.sh" sweep
}

servers_may_exist=0

teardown_on_exit() {
	local status=$?
	if ((servers_may_exist)); then
		log "tear down"
		"$self" down || status=1
	fi
	exit "$status"
}

cmd_all() {
	trap teardown_on_exit EXIT
	trap 'exit 130' INT
	trap 'exit 143' TERM
	servers_may_exist=1
	"$self" up
	"$self" provision
	"$self" test
}

(($# == 1)) || usage
case $1 in
-h | --help)
	print_usage
	exit 0
	;;
up | test | down | sweep)
	require_inputs 0
	;;
provision | all)
	require_inputs 1
	;;
*) usage ;;
esac
"cmd_$1"
