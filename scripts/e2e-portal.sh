#!/usr/bin/env bash
# The E2E_AWS_HOOK for scripts/e2e-aws.sh. The steps and inputs are in
# docs/portal-deployment.md, "End-to-end test".
#
# Everything runs in the portal image built on core, so the caller needs only
# ssh, git and tar.

# The bash -c programs expand their own arguments on the remote host.
# shellcheck disable=SC2016

set -euo pipefail

repo=$(cd "$(dirname "$0")/.." && pwd)

: "${CORE_HOST:?}" "${WORKER_HOST:?}" "${ADMIN_USER:?}" "${E2E_PROXY_ENV:?}"
job_source=${E2E_JOB_SOURCE:-osiptel}
# SUNAT's own RUC, a public document.
job_document=${E2E_JOB_DOCUMENT:-20131312955}

deployment=portal-e2e
worker=worker
remote_dir=/opt/docs-pe-e2e
dokploy_url=http://127.0.0.1:3000

# shellcheck source=/dev/null
source "$repo/.data/vps-setup/tests/e2e/known-hosts.sh"

umask 077
state=${TMPDIR:-/tmp}/docs-pe-e2e/$CORE_HOST
mkdir -p "$state"

current_step=start

log() {
	printf '==> %s\n' "$*" >&2
}

die() {
	printf 'error: %s\n' "$*" >&2
	exit 1
}

step() {
	current_step=$1
	log "$1"
}

report_failure() {
	local status=$?
	((status == 0)) || printf 'e2e-portal: failed at step: %s\n' "$current_step" >&2
	exit "$status"
}
trap report_failure EXIT

ssh_to() {
	local host=$1
	shift
	ssh -o BatchMode=yes -o ConnectTimeout=15 -o StrictHostKeyChecking=yes \
		-o "UserKnownHostsFile=$(e2e_known_hosts "$host")" "$ADMIN_USER@$host" "$@"
}

# Runs a command as root on host. The variables named before -- are exported
# to it through the script on stdin, so their values never reach a command
# line. The command cannot read the caller's stdin.
on() {
	local host=$1 script=''
	shift
	while [[ $1 != -- ]]; do
		script+=$(printf 'export %s=%q' "$1" "${!1}")$'\n'
		shift
	done
	shift
	script+=$(printf '%q ' "$@")
	ssh_to "$host" 'sudo bash -s' <<<"$script"
}

# Writes the caller's stdin to path on host with mode, replacing the file by
# rename. uutils install 0.10.0 (Ubuntu 26.04) cannot write /dev/stdin over an
# existing file. The program must not contain a single quote.
upload() {
	local program='
		set -euo pipefail
		mkdir -p -m 755 "${0%/*}"
		tmp=$(mktemp "$0.XXXXXX")
		trap "rm -f \"\$tmp\"" EXIT
		cat >"$tmp"
		chmod "$1" "$tmp"
		mv -f "$tmp" "$0"
	'
	ssh_to "$1" "sudo bash -c '$program' $(printf '%q %q' "$2" "$3")"
}

hex() {
	od -An -N"$1" -tx1 /dev/urandom | tr -d ' \n'
}

# Sets a variable and keeps it for the next run against this core.
remember() {
	printf -v "$1" '%s' "$2"
	printf '%s=%q\n' "$1" "$2" >>"$state/values"
}

read_proxy_env() {
	local key value
	[[ -r $E2E_PROXY_ENV ]] || die "E2E_PROXY_ENV ($E2E_PROXY_ENV) is not a readable file"
	while IFS='=' read -r key value; do
		case $key in
		PORTAL_PROVISION_GEONODE_USERNAME | PORTAL_PROVISION_GEONODE_PASSWORD)
			printf -v "$key" '%s' "$value"
			;;
		esac
	done <"$E2E_PROXY_ENV"
	[[ -n ${PORTAL_PROVISION_GEONODE_USERNAME:-} && -n ${PORTAL_PROVISION_GEONODE_PASSWORD:-} ]] ||
		die "E2E_PROXY_ENV must set PORTAL_PROVISION_GEONODE_USERNAME and PORTAL_PROVISION_GEONODE_PASSWORD"
}

write_core_env() {
	cat <<-EOF
		POSTGRES_DB=portal
		POSTGRES_USER=portal
		POSTGRES_PASSWORD=$(hex 32)
		GARAGE_RPC_SECRET=$(hex 32)
		GARAGE_DEFAULT_ACCESS_KEY=GK$(hex 12)
		GARAGE_DEFAULT_SECRET_KEY=$(hex 32)
		GARAGE_DEFAULT_BUCKET=portal-objects
		PORTAL_PUBLIC_ORIGIN=https://$core_dns
		PORTAL_TURNSTILE_SITE_KEY=1x00000000000000000000AA
		PORTAL_TURNSTILE_SECRET=1x0000000000000000000000000000000AA
		PORTAL_RESEND_API_KEY=re_e2e_placeholder
		PORTAL_MAIL_FROM=portal-e2e@example.com
		PORTAL_WORKER_BOOTSTRAP_TOKEN=$(hex 32)
		PORTAL_WORKER_DATABASE_ADDRESS=$core_ip:5432
	EOF
}

# Dokploy answers only on core's loopback, so ops/deploy.py runs on core with
# host networking.
deploy_tool() {
	on "$CORE_HOST" DOKPLOY_SESSION_COOKIE -- docker run --rm --network host --user 0:0 \
		-v "$remote_dir/src:/src:ro" -v "$remote_dir/core.env:/run/core.env:ro" -w /src \
		-e "DOKPLOY_URL=$dokploy_url" -e DOKPLOY_SESSION_COOKIE \
		"$image" python -m ops.deploy "$@"
}

# The driver runs on worker, a tailnet peer of core, so its requests reach the
# portal through `tailscale serve` as a browser's would.
portal_driver() {
	on "$WORKER_HOST" E2E_ADMIN_EMAIL E2E_ADMIN_PASSWORD E2E_ADMIN_TOTP_SECRET -- \
		docker run --rm -e E2E_ADMIN_EMAIL -e E2E_ADMIN_PASSWORD -e E2E_ADMIN_TOTP_SECRET \
		-v "$remote_dir/e2e_portal.py:/e2e_portal.py:ro" \
		"$image" python /e2e_portal.py "$1" "https://$core_dns" "$core_ip:443" "${@:2}"
}

# The database checks read the portal's settings, so the driver runs inside the
# web container.
web_driver() {
	on "$CORE_HOST" -- bash -c 'docker exec -i "$1" python - "${@:2}" <"$0"' \
		"$remote_dir/src/ops/e2e_portal.py" "$web" "$@"
}

provision() {
	on "$CORE_HOST" E2E_ADMIN_PASSWORD PORTAL_PROVISION_GEONODE_USERNAME PORTAL_PROVISION_GEONODE_PASSWORD -- \
		docker exec -e E2E_ADMIN_PASSWORD -e PORTAL_PROVISION_GEONODE_USERNAME -e PORTAL_PROVISION_GEONODE_PASSWORD \
		"$web" portal-admin provision --admin-email "$E2E_ADMIN_EMAIL" \
		--admin-password-env E2E_ADMIN_PASSWORD --team-name E2E --team-slug e2e \
		--proxy-provider geonode --proxy-label e2e >&2
}

compose_app() {
	deploy_tool list "$deployment" | awk -F '\t' -v name="$1" '$1 == "compose" && $2 == name { print $3 }'
}

read_proxy_env
if [[ -f $state/values ]]; then
	# shellcheck source=/dev/null
	source "$state/values"
fi
E2E_ADMIN_TOTP_SECRET=${E2E_ADMIN_TOTP_SECRET:-}

step "read tailnet addresses"
core_ip=$(on "$CORE_HOST" -- tailscale ip -4)
worker_ip=$(on "$WORKER_HOST" -- tailscale ip -4)
core_dns=$(on "$CORE_HOST" -- bash -c 'tailscale status --json | python3 -c "$0"' \
	'import json, sys; print(json.load(sys.stdin)["Self"]["DNSName"].rstrip("."))')
[[ -n $core_ip && -n $worker_ip && -n $core_dns ]] ||
	die "could not read the tailnet addresses (core $core_ip $core_dns, worker $worker_ip)"
log "core $core_dns ($core_ip), worker $worker_ip"

step "build the portal image on core"
(cd "$repo" && git ls-files -z --cached --others --exclude-standard |
	while IFS= read -r -d '' path; do
		[[ -f $path ]] && printf '%s\0' "$path"
	done |
	tar --null -T - -cf -) |
	ssh_to "$CORE_HOST" "sudo rm -rf $remote_dir/src && sudo install -d -m 755 $remote_dir $remote_dir/src && sudo tar -x --no-same-owner -C $remote_dir/src"
on "$CORE_HOST" -- docker build --progress=plain -f "$remote_dir/src/packages/portal/Dockerfile" \
	-t docs-pe-portal:e2e-build "$remote_dir/src" >&2
# Tagged by content, so an unchanged tree leaves every compose unchanged and a
# changed one redeploys.
image_id=$(on "$CORE_HOST" -- docker image inspect -f '{{.Id}}' docs-pe-portal:e2e-build)
image=docs-pe-portal:e2e-${image_id:7:12}
on "$CORE_HOST" -- docker tag docs-pe-portal:e2e-build "$image"
log "image $image"

step "load the image on worker"
# Core to worker as root is the path Dokploy itself uses, so this also shows
# that the server the deploy tool registers is reachable.
on "$CORE_HOST" -- bash -c '
	to_worker() {
		ssh -o BatchMode=yes -o StrictHostKeyChecking=accept-new \
			-o "UserKnownHostsFile=$1/known_hosts" "root@$2" "${@:3}"
	}
	if ! to_worker "$1" "$2" docker image inspect "$0" </dev/null >/dev/null 2>&1; then
		docker save "$0" | to_worker "$1" "$2" docker load >&2
	fi' "$image" "$remote_dir" "$worker_ip"
# The portal image runs as a non-root user, which must read the driver.
upload "$WORKER_HOST" "$remote_dir/e2e_portal.py" 644 <"$repo/ops/e2e_portal.py"

step "create Dokploy's administrator and sign in"
if [[ -z ${DOKPLOY_SESSION_COOKIE:-} ]]; then
	# Dokploy accepts only one sign-up, so the credentials are kept only once
	# the session exists. A failure here needs fresh servers.
	E2E_DOKPLOY_EMAIL=e2e-$(hex 4)@example.com
	E2E_DOKPLOY_PASSWORD=$(hex 24)
	cookie=$(on "$CORE_HOST" E2E_DOKPLOY_EMAIL E2E_DOKPLOY_PASSWORD -- \
		docker run --rm --network host -e "DOKPLOY_URL=$dokploy_url" \
		-e E2E_DOKPLOY_EMAIL -e E2E_DOKPLOY_PASSWORD \
		-v "$remote_dir/src/ops/e2e_portal.py:/e2e_portal.py:ro" \
		"$image" python /e2e_portal.py dokploy-session)
	remember E2E_DOKPLOY_EMAIL "$E2E_DOKPLOY_EMAIL"
	remember E2E_DOKPLOY_PASSWORD "$E2E_DOKPLOY_PASSWORD"
	remember DOKPLOY_SESSION_COOKIE "$cookie"
fi

step "write the deployment's secrets"
if [[ ! -f $state/core.env ]]; then
	write_core_env >"$state/core.env"
	remember E2E_ADMIN_EMAIL "admin-$(hex 4)@example.com"
	remember E2E_ADMIN_PASSWORD "$(hex 24)"
fi
upload "$CORE_HOST" "$remote_dir/core.env" 600 <"$state/core.env"

step "serve the portal over tailnet HTTPS"
# Dokploy's Traefik routes the portal's domain on port 80. tailscale serve
# terminates TLS with the tailnet certificate for core's name, and waits for a
# console approval when the tailnet has HTTPS or Serve disabled.
on "$CORE_HOST" -- timeout 120 tailscale serve --bg --https=443 http://127.0.0.1:80 >&2

step "deploy"
deploy_tool deploy "$deployment" --image "$image" --env-file /run/core.env \
	--worker "$worker=$worker_ip" >&2

step "deploy again, changing nothing"
second=$(deploy_tool deploy "$deployment" --image "$image" --env-file /run/core.env \
	--worker "$worker=$worker_ip")
printf '%s\n' "$second" >&2
if grep -qv '^ok ' <<<"$second"; then
	die "the second deploy changed something"
fi

step "find the web container"
core_app=$(compose_app core)
[[ -n $core_app ]] || die "ops.deploy list shows no core compose"
web=$(on "$CORE_HOST" -- docker ps -q \
	--filter "label=com.docker.compose.project=$core_app" \
	--filter label=com.docker.compose.service=web)
[[ -n $web && $web != *$'\n'* ]] || die "expected one running web container for $core_app, found: ${web:-none}"

step "provision the first administrator"
provision

step "enrol the administrator's TOTP"
if [[ -z $E2E_ADMIN_TOTP_SECRET ]]; then
	secret=$(portal_driver enrol-totp)
	remember E2E_ADMIN_TOTP_SECRET "$secret"
fi

step "provision the team and proxy credential"
provision

step "submit a $job_source lookup of $job_document"
job=$(portal_driver submit "$job_source" "$job_document")
log "job $job"

step "wait for $worker to complete job $job and store its result"
web_driver check-job "$job" "$worker" >&2

step "remove $worker"
deploy_tool remove-worker "$deployment" "$worker" >&2
web_driver check-revoked "$worker" >&2
remaining=$(deploy_tool list "$deployment")
if grep -qE $'^server\t|^compose\tworker-' <<<"$remaining"; then
	printf '%s\n' "$remaining" >&2
	die "$worker's server or compose is still in Dokploy"
fi

log "portal e2e passed"
