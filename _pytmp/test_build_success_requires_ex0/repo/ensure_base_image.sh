#!/usr/bin/env bash
printf 'probe base_image=%s mirrors=%s\n' "${BASE_IMAGE-}" "${FNMUSIC_DOCKER_MIRRORS-}" >> "${PROBE_LOG}"
[ "${PROBE_RC:-0}" -eq 0 ] || exit 1
printf "FNMUSIC_BASE_IMAGE='python:3.13-slim'\n" >> "${BASE_DIR}/.env"
