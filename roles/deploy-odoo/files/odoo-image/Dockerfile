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
#   - `packaging` - not imported by any addon directly; Odoo's own
#     odoo/modules/module.py needs `packaging.requirements.Requirement` to
#     parse any `external_dependencies["python"]` entry that carries a
#     version specifier or an extra (fs_storage's `fsspec>=2024.5.0`,
#     fs_attachment's `fsspec>=2025.3.0`, fs_attachment_s3's `fsspec[s3]`
#     all do), and falls back to a shim that unconditionally raises
#     "Package `packaging` is required to parse ... external dependency
#     and is not installed" for any of those when it's missing - confirmed
#     live, this broke every tenant init. Stock odoo:19.0 doesn't ship it
#     (Odoo treats it as optional); do not remove this thinking it's
#     unused - nothing here `import`s it, Odoo's own dependency-check code
#     does.
#
# Built + pushed automatically by tasks/_ensure-odoo-image.yml (only when
# the content-derived tag - see that task's own comment - doesn't already
# exist in the registry, and only when odoo_image_build.enabled is true -
# see README "Custom Odoo image").
ARG ODOO_VERSION
FROM odoo:${ODOO_VERSION}.0

USER root
# Try the direct install first (python3-pip is present on most recent
# odoo images); fall back to installing pip if it's missing.
RUN pip3 install --no-cache-dir --break-system-packages redis "fsspec[s3]>=2025.3.0" python_slugify packaging \
    || (apt-get update \
        && apt-get install -y --no-install-recommends python3-pip \
        && pip3 install --no-cache-dir --break-system-packages redis "fsspec[s3]>=2025.3.0" python_slugify packaging \
        && rm -rf /var/lib/apt/lists/*)
USER odoo
