# Derived image: ERPNext + the Tally Migrator app baked in.
#
# Why a custom image: frappe_docker's pwd.yml mounts only the `sites` volume,
# so app CODE (apps/ + env/) lives in each container's own filesystem. On any
# container recreate the app vanishes and `sites/apps.txt` resets to the image
# default (frappe + erpnext). Baking the app — and adding it to the image's
# apps.txt — makes it survive recreates across ALL python services at once.
#
# Build (must match the base image platform, amd64, even on Apple Silicon):
#   docker build --platform linux/amd64 -t frappe/erpnext-tally:v16.21.1 .
ARG BASE=frappe/erpnext:v16.21.1
FROM ${BASE}

USER frappe
WORKDIR /home/frappe/frappe-bench

# Copy the app into the bench's apps/ directory.
COPY --chown=frappe:frappe . apps/tally_migrator

# Editable install into the bench's own Python env, then register the app in
# the image-level apps.txt so the configurator/create-site include it.
# The base image's apps.txt has no trailing newline, so guard against
# concatenating onto the last line (`sed '$a\'` ensures a final newline).
RUN env/bin/pip install --no-cache-dir -e apps/tally_migrator \
    && ( grep -qxF tally_migrator sites/apps.txt \
         || { sed -i -e '$a\' sites/apps.txt; echo tally_migrator >> sites/apps.txt; } )
