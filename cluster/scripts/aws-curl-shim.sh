#!/bin/bash
# A stand-in for the `aws` CLI, implementing only the three commands nixlbench
# shells out to when it benchmarks the OBJ backend:
#
#   aws s3 cp <file> s3://<bucket>        seed an object before a READ run
#   aws s3 cp s3://<bucket>/<key> <file>  read one back
#   aws s3 rm s3://<bucket>/<key>         clean up afterwards
#
# WHY THIS EXISTS
#
# nixlbench cannot run a READ against the OBJ backend without first putting an
# object there, and it does that by shelling out to the real CLI rather than
# through NIXL. Against rocm-ernic's `--backend s3` endpoint that fails before
# it starts: aws-cli v2 sends a streaming, chunk-signed PutObject, and the
# server's HTTP parser -- a few hundred lines whose job is to exercise the RDMA
# data path, not to be a gateway -- answers InvalidRequest. Relaxing the
# checksum settings does not help, because the framing is the problem.
#
# curl works, because the endpoint's non-RDMA path is an ordinary S3 server
# with the payload in the body, and the docs say so: that path is there exactly
# so the control plane can be checked without RDMA. So this translates the
# three calls into curl and nothing else.
#
# It touches only the SEEDING. The measured transfer still goes through NIXL,
# hipObject and the x-amz-rdma-token path; nothing here is in the data path or
# in any timing.
#
# AWS_ENDPOINT_URL (or --endpoint-url) says where to send them.
set -euo pipefail

endpoint="${AWS_ENDPOINT_URL:-}"

args=()
while [[ $# -gt 0 ]]; do
	case "$1" in
		--endpoint-url) endpoint="$2"; shift 2 ;;
		--endpoint-url=*) endpoint="${1#*=}"; shift ;;
		# Every other global flag the CLI accepts is irrelevant here --
		# there is no credential, region or output format to honour.
		--region|--profile|--output) shift 2 ;;
		--*) shift ;;
		*) args+=("$1"); shift ;;
	esac
done

[[ -n "${endpoint}" ]] || { echo "aws-shim: no endpoint; set AWS_ENDPOINT_URL" >&2; exit 2; }
endpoint="${endpoint%/}"

# s3://bucket/key -> bucket/key, so it can be pasted onto the endpoint.
strip_s3() { printf '%s' "${1#s3://}"; }

# The body path has a hard ceiling: rocm-ernic caps a whole HTTP request,
# headers and body together, at S3_HTTP_REQUEST_MAX = 16384 bytes, and answers
# 400 for anything past it. That is deliberate -- the body path exists to
# debug the control plane, and the RDMA path is where size belongs -- but
# nixlbench needs a seed object as large as the biggest block it will read, so
# anything over this goes up as a multipart upload instead. 15360 leaves room
# for the request line and headers, and the server takes parts 1..1024, which
# puts the ceiling at 15 MiB.
PART=15360

multipart_put() {
	local src="$1" path="$2" size="$3" upload_id part=1 off=0 code
	upload_id="$(curl -sS -X POST "${endpoint}/${path}?uploads" \
		| sed -n 's|.*<UploadId>\(.*\)</UploadId>.*|\1|p')"
	[[ -n "${upload_id}" ]] || { echo "aws-shim: no UploadId" >&2; exit 1; }

	while (( off < size )); do
		# One part at a time rather than `split`, so no temporary tree
		# is created for an object that may be several hundred parts.
		# skip_bytes/count_bytes so bs stays a sane block size instead
		# of 1, which would make this hundreds of thousands of reads.
		dd if="${src}" iflag=skip_bytes,count_bytes bs=64K \
			skip="${off}" count="${PART}" \
			of=/tmp/.aws-shim-part status=none
		code="$(curl -sS -o /dev/null -w '%{http_code}' -H 'Expect:' \
			-X PUT --data-binary @/tmp/.aws-shim-part \
			"${endpoint}/${path}?partNumber=${part}&uploadId=${upload_id}")"
		[[ "${code}" =~ ^2 ]] || {
			echo "aws-shim: part ${part} HTTP ${code}" >&2; exit 1; }
		off=$(( off + PART ))
		part=$(( part + 1 ))
	done
	rm -f /tmp/.aws-shim-part

	code="$(curl -sS -o /dev/null -w '%{http_code}' -H 'Expect:' \
		-X POST --data-binary '' \
		"${endpoint}/${path}?uploadId=${upload_id}")"
	[[ "${code}" =~ ^2 ]] || {
		echo "aws-shim: complete HTTP ${code}" >&2; exit 1; }
}

[[ "${args[0]:-}" == "s3" ]] || { echo "aws-shim: only 's3' is implemented" >&2; exit 2; }

case "${args[1]:-}" in
	cp)
		src="${args[2]:?}" dst="${args[3]:?}"
		if [[ "${dst}" == s3://* ]]; then
			# `aws s3 cp <file> s3://<bucket>` keeps the basename.
			path="$(strip_s3 "${dst}")"
			[[ "${path}" == */* ]] || path="${path}/$(basename "${src}")"
			size="$(stat -c %s "${src}")"
			if (( size > PART )); then
				multipart_put "${src}" "${path}" "${size}"
				code=200
			else
				code="$(curl -sS -o /dev/null -w '%{http_code}' \
					-H 'Expect:' \
					-X PUT --data-binary "@${src}" "${endpoint}/${path}")"
			fi
		else
			code="$(curl -sS -o "${dst}" -w '%{http_code}' \
				"${endpoint}/$(strip_s3 "${src}")")"
		fi
		;;
	rm)
		code="$(curl -sS -o /dev/null -w '%{http_code}' \
			-X DELETE "${endpoint}/$(strip_s3 "${args[2]:?}")")"
		;;
	*)
		echo "aws-shim: 's3 ${args[1]:-}' is not implemented" >&2
		exit 2
		;;
esac

# A 2xx is the only success. Reporting the code rather than swallowing it
# matters: nixlbench only prints "Failed to put object" and carries on, so a
# silent shim would turn a seeding failure into a mysterious registerMem error.
[[ "${code}" =~ ^2 ]] || { echo "aws-shim: HTTP ${code}" >&2; exit 1; }
