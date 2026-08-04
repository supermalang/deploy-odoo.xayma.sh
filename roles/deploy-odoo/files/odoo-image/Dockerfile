# Layers Python packages onto the stock Odoo image that its addons need but
# the stock image doesn't ship:
#   - `redis` - session_redis (mandatory, see README "Architecture"/"Known
#     caveats") needs to `import redis`.
#   - `fsspec[s3]` + `python_slugify` - fs_storage/fs_attachment/
#     fs_attachment_s3 (mandatory, see tasks/_resolve-plan-spec.yml).
#     `fsspec[s3]` pulls in `s3fs` (itself on `aiobotocore`/`botocore`) -
#     no separate `boto3` install needed. Version floor is
#     fs_attachment's (the strictest of the three at the time this was
#     written - bump if a future OCA release raises it further).
#
# Built + pushed automatically by tasks/_ensure-odoo-image.yml (only when
# `{{ odoo_image_repo }}:{{ version }}.0` doesn't already exist in the
# registry, and only when odoo_image_build.enabled is true - see README
# "Custom Odoo image"). Not built/tested against a real Docker daemon as
# part of this change (none was available in the environment that wrote
# it) - verify the first real build actually starts a working container
# before relying on it.
ARG ODOO_VERSION
FROM odoo:${ODOO_VERSION}.0

USER root
# Try the direct install first (python3-pip is present on most recent
# odoo images); fall back to installing pip if it's missing.
RUN pip3 install --no-cache-dir --break-system-packages redis "fsspec[s3]>=2025.3.0" python_slugify \
    || (apt-get update \
        && apt-get install -y --no-install-recommends python3-pip \
        && pip3 install --no-cache-dir --break-system-packages redis "fsspec[s3]>=2025.3.0" python_slugify \
        && rm -rf /var/lib/apt/lists/*)
USER odoo
