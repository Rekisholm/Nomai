# Based on the Moodle image currently in use
FROM mrzero03/moodle:4.3.5

# Switch to root to install extensions
USER root

# 1. Install build dependencies; libffi-dev is required to compile the PHP FFI extension
RUN apt-get update && apt-get install -y \
    libffi-dev \
    && rm -rf /var/lib/apt/lists/*

# 2. Install and enable the FFI extension
RUN docker-php-ext-configure ffi --with-ffi \
    && docker-php-ext-install ffi

# 3. Configure PHP to allow FFI
# By default ffi.enable is "preload" (only preload scripts can use it); change it to "true" for all scripts
RUN echo "ffi.enable = true" >> /usr/local/etc/php/conf.d/docker-php-ext-ffi.ini
