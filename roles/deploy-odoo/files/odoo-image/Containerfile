# Layers the `redis` Python package onto the stock Odoo image, so
# session_redis (mandatory - see README "Architecture"/"Known caveats")
# can actually `import redis` - the stock image doesn't ship it.
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
RUN pip3 install --no-cache-dir --break-system-packages redis \
    || (apt-get update \
        && apt-get install -y --no-install-recommends python3-pip \
        && pip3 install --no-cache-dir --break-system-packages redis \
        && rm -rf /var/lib/apt/lists/*)
USER odoo
