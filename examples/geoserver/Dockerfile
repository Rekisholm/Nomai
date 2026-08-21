FROM pg-request-logger:13.12

# Switch to root to install packages
USER root

# Install the PostGIS extension
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
    postgresql-13-postgis-3 \
    postgresql-13-postgis-3-scripts \
    && rm -rf /var/lib/apt/lists/*

# Switch back to the postgres user
USER postgres