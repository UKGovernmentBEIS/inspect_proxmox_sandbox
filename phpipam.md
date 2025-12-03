# Installing and Configuring phpIPAM on Proxmox VE

This guide covers installing phpIPAM directly on a Proxmox VE server and integrating it with Proxmox SDN for IP address management, including support for overlapping IP ranges across different zones.

## Table of Contents

- [Overview](#overview)
- [Prerequisites](#prerequisites)
- [Installation](#installation)
  - [Install Dependencies](#install-dependencies)
  - [Install phpIPAM](#install-phpipam)
  - [Configure Database](#configure-database)
  - [Configure Apache](#configure-apache)
- [phpIPAM Configuration](#phpipam-configuration)
  - [Enable API Access](#enable-api-access)
  - [Configure API Security](#configure-api-security)
  - [Create API Application](#create-api-application)
- [Proxmox SDN Integration](#proxmox-sdn-integration)
  - [Create IPAM Configuration](#create-ipam-configuration)
  - [Configure SDN Zones](#configure-sdn-zones)
  - [Add Subnets](#add-subnets)
  - [Apply Configuration](#apply-configuration)
- [Verification](#verification)
- [Troubleshooting](#troubleshooting)
- [Security Considerations](#security-considerations)

---

## Overview

phpIPAM is an open-source IP address management (IPAM) application that provides centralized IP address tracking. When integrated with Proxmox VE SDN, it offers several advantages over the built-in IPAM:

- **Centralized Management**: Single source of truth for IP addresses across multiple zones
- **Overlapping IP Support**: Properly handles overlapping IP ranges in different SDN zones
- **Advanced Features**: VLAN management, subnet hierarchies, custom fields, and more
- **Web Interface**: User-friendly web UI for IP management

### Use Case: Overlapping IP Ranges

Proxmox's built-in IPAM does not support overlapping IP ranges across different simple zones. This is problematic for scenarios like:
- Testing environments with identical network configurations
- Multi-tenant environments where tenants use the same private IP ranges
- Development/staging/production environments with matching IP schemes

phpIPAM solves this by maintaining context-aware IP allocation.

---

## Prerequisites

- Proxmox VE 9.x (tested on 9.1.1)
- Root access to the Proxmox server
- At least 2GB free disk space
- Basic knowledge of Linux command line

---

## Installation

### Install Dependencies

SSH into your Proxmox server and install the required packages:

```bash
# Update package list
apt-get update

# Install Apache, MariaDB, PHP and required modules
apt-get install -y \
  apache2 \
  mariadb-server \
  php \
  php-mysql \
  php-curl \
  php-gd \
  php-intl \
  php-mbstring \
  php-gmp \
  php-json \
  php-xml \
  php-fpm \
  git \
  libapache2-mod-php
```

### Install phpIPAM

Clone phpIPAM from GitHub and check out the stable 1.6 branch:

```bash
# Navigate to web root
cd /var/www

# Clone phpIPAM repository
git clone --recursive https://github.com/phpipam/phpipam.git

# Switch to stable version
cd phpipam
git checkout 1.6
```

### Configure Database

Create the database and user for phpIPAM:

```bash
mysql -e "CREATE DATABASE phpipam;"
mysql -e "GRANT ALL PRIVILEGES ON phpipam.* TO 'phpipam'@'localhost' IDENTIFIED BY 'phpipam123';"
mysql -e "FLUSH PRIVILEGES;"
```

**Note:** Change `phpipam123` to a secure password in production environments.

Configure phpIPAM database settings:

```bash
cd /var/www/phpipam
cp config.dist.php config.php

# Edit database credentials
sed -i "s/\$db\['pass'\] = 'phpipamadmin';/\$db['pass'] = 'phpipam123';/" config.php
```

Import the database schema:

```bash
mysql phpipam < db/SCHEMA.sql
```

### Configure Apache

Create an Apache virtual host for phpIPAM:

```bash
cat > /etc/apache2/sites-available/phpipam.conf << 'EOF'
<VirtualHost *:80>
    ServerAdmin admin@example.com
    DocumentRoot /var/www/phpipam
    ServerName phpipam.local

    <Directory /var/www/phpipam>
        Options Indexes FollowSymLinks MultiViews
        AllowOverride All
        Require all granted
    </Directory>

    ErrorLog ${APACHE_LOG_DIR}/error.log
    CustomLog ${APACHE_LOG_DIR}/access.log combined
</VirtualHost>
EOF
```

Enable the site and Apache modules:

```bash
# Set proper permissions
chown -R www-data:www-data /var/www/phpipam
chmod -R 755 /var/www/phpipam

# Disable default site (optional)
a2dissite 000-default

# Enable phpIPAM site
a2ensite phpipam

# Enable mod_rewrite
a2enmod rewrite

# Restart Apache
systemctl restart apache2
```

---

## phpIPAM Configuration

### Enable API Access

Edit the phpIPAM configuration to allow API access:

```bash
# Enable unsafe API access (required for non-SSL connections)
sed -i 's/$api_allow_unsafe = false;/$api_allow_unsafe = true;/' /var/www/phpipam/config.php

# Enable API in database
mysql phpipam -e "UPDATE settings SET api=1 WHERE id=1;"
```

**Security Warning:** `api_allow_unsafe` allows non-SSL API connections. In production, you should use HTTPS with proper SSL certificates.

### Configure API Security

For integration with Proxmox on the same server, we'll use the "none" security mode. This requires modifying the phpIPAM API to skip authentication for the 'none' security mode.

Edit `/var/www/phpipam/api/index.php` around line 169:

```bash
# Find the line that says:
#   if($app->app_security=="ssl_token" || $app->app_security=="none") {
# And change it to:
#   if($app->app_security=="ssl_token") {

sed -i '169s/.*/\t\tif($app->app_security=="ssl_token") {/' /var/www/phpipam/api/index.php
```

This modification removes the authentication requirement for API applications using 'none' security mode.

### Create API Application

Add an API application for Proxmox:

```bash
mysql phpipam -e "INSERT INTO api (app_id, app_code, app_permissions, app_comment, app_security) \
  VALUES ('proxmox', 'proxmox_api_token', 2, 'Proxmox VE SDN Integration', 'none');"
```

Parameters explained:
- `app_id`: Unique identifier (used in API URL path)
- `app_code`: API token (not used in 'none' mode)
- `app_permissions`: 2 = read/write
- `app_security`: 'none' = no authentication (local only)

Verify the API is working:

```bash
curl -s http://localhost/api/proxmox/sections/1/
```

You should see a JSON response with section information.

---

## Proxmox SDN Integration

### Create IPAM Configuration

Configure Proxmox to use phpIPAM as an IPAM provider:

```bash
pvesh create /cluster/sdn/ipams \
  --ipam phpipam1 \
  --type phpipam \
  --url http://localhost/api/proxmox \
  --token dummy \
  --section 1
```

Parameters:
- `--ipam phpipam1`: Name of the IPAM configuration
- `--type phpipam`: IPAM type
- `--url`: API endpoint (note: includes `/api/proxmox` where 'proxmox' is the app_id)
- `--token`: Required parameter (not used in 'none' security mode)
- `--section 1`: phpIPAM section ID (default "Customers" section)

Verify the IPAM was created:

```bash
pvesh get /cluster/sdn/ipams
```

### Configure SDN Zones

Update existing zones or create new zones to use phpIPAM:

```bash
# Create a new simple zone with phpIPAM
pvesh create /cluster/sdn/zones \
  --zone zone1 \
  --type simple \
  --ipam phpipam1 \
  --dhcp dnsmasq

# Or update an existing zone
pvesh set /cluster/sdn/zones/zone1 --ipam phpipam1
```

**Important:** You cannot change the IPAM of a zone that already has subnets defined. Delete subnets first if needed.

### Add Subnets

Create virtual networks (vnets) and subnets:

```bash
# Create vnet in zone1
pvesh create /cluster/sdn/vnets \
  --vnet vnet1 \
  --zone zone1

# Add subnet to vnet1
pvesh create /cluster/sdn/vnets/vnet1/subnets \
  --subnet 192.168.100.0/24 \
  --type subnet \
  --gateway 192.168.100.1 \
  --dhcp-range start-address=192.168.100.100,end-address=192.168.100.200
```

### Apply Configuration

Apply the SDN configuration to activate the changes:

```bash
pvesh set /cluster/sdn
```

This will:
1. Create network interfaces
2. Register subnets in phpIPAM
3. Register gateway IPs in phpIPAM
4. Configure DHCP if enabled

---

## Verification

### Check Network Interfaces

Verify that the virtual network interfaces were created:

```bash
ip addr show | grep vnet
```

Expected output:
```
5: vnet1: <BROADCAST,MULTICAST,UP,LOWER_UP> mtu 1500 qdisc noqueue state UNKNOWN
    inet 192.168.100.1/24 scope global vnet1
```

### Check phpIPAM Subnets

Query the phpIPAM API to verify subnets were registered:

```bash
curl -s http://localhost/api/proxmox/subnets/ | python3 -m json.tool
```

You should see your subnet in the response with the correct network and mask.

### Check IP Allocations

Query IP addresses in a specific subnet:

```bash
# First, find your subnet ID from the subnets query above
# Then query addresses (replace 7 with your subnet ID)
curl -s http://localhost/api/proxmox/subnets/7/addresses/ | python3 -m json.tool
```

You should see the gateway IP registered.

### Check Apache Logs

Monitor API communication between Proxmox and phpIPAM:

```bash
tail -f /var/log/apache2/access.log | grep proxmox
```

Successful operations will show:
- `POST /api/proxmox/subnets/` → 201 (subnet created)
- `POST /api/proxmox/addresses/` → 201 (IP allocated)
- `DELETE /api/proxmox/addresses/` → 200 (IP released)

---

## Troubleshooting

### API Returns "SSL connection is required for API"

**Cause:** `api_allow_unsafe` is not set to `true` in config.php.

**Solution:**
```bash
grep api_allow_unsafe /var/www/phpipam/config.php
# Should show: $api_allow_unsafe = true;

# If not, edit the file:
sed -i 's/$api_allow_unsafe = false;/$api_allow_unsafe = true;/' /var/www/phpipam/config.php
```

### API Returns "401 Unauthorized" with 'none' security

**Cause:** The API authentication check was not properly disabled for 'none' mode.

**Solution:** Verify the modification in `/var/www/phpipam/api/index.php` around line 169. The authentication check should only apply to `ssl_token`, not `none`:

```php
if($app->app_security=="ssl_token") {
    // start auth class and validate connection
    require_once( dirname(__FILE__) . '/controllers/User.php');
    $Authentication = new User_controller ($Database, $Tools, $Params, $Response);
    $Authentication->check_auth ();
}
```

### API Returns "400 Bad Request" or "Invalid response from server"

**Cause:** The URL doesn't include the app_id in the path.

**Solution:** Ensure the IPAM URL is `http://localhost/api/proxmox` (not just `http://localhost/api`). The `proxmox` part must match the `app_id` in the phpIPAM API table.

### Cannot Change Zone IPAM

**Cause:** Zone already has subnets defined.

**Solution:** Delete all subnets from the zone first:
```bash
# List subnets
pvesh get /cluster/sdn/vnets/vnet1/subnets

# Delete subnet (use the correct ID from the list)
pvesh delete /cluster/sdn/vnets/vnet1/subnets/zone1-192.168.100.0-24

# Now you can change the IPAM
pvesh set /cluster/sdn/zones/zone1 --ipam phpipam1
```

### Subnet Not Appearing in phpIPAM

**Cause:** SDN configuration was not applied.

**Solution:**
```bash
# Apply SDN configuration
pvesh set /cluster/sdn

# Check Proxmox logs
journalctl -u pve-cluster -f
```

### Apache/PHP Errors

Check the Apache error log for PHP errors:

```bash
tail -50 /var/log/apache2/error.log
```

Common issues:
- Missing PHP modules: Install the required module
- Permission errors: Ensure `/var/www/phpipam` is owned by `www-data`
- Database connection errors: Verify credentials in `config.php`

---

## Security Considerations

### Production Deployments

The configuration described in this guide uses **unsafe API access** suitable for local/development environments. For production deployments, you should:

1. **Use HTTPS with SSL certificates:**
   ```bash
   # Install Let's Encrypt certbot
   apt-get install -y certbot python3-certbot-apache

   # Get certificate
   certbot --apache -d your-domain.com
   ```

2. **Change API security mode to `ssl_token`:**
   ```bash
   mysql phpipam -e "UPDATE api SET app_security='ssl_token' WHERE app_id='proxmox';"
   ```

3. **Disable `api_allow_unsafe`:**
   ```bash
   sed -i 's/$api_allow_unsafe = true;/$api_allow_unsafe = false;/' /var/www/phpipam/config.php
   ```

4. **Use strong database passwords:**
   ```bash
   mysql -e "ALTER USER 'phpipam'@'localhost' IDENTIFIED BY 'strong_random_password';"
   # Update config.php with new password
   ```

5. **Restrict Apache access (if phpIPAM is only for Proxmox):**
   ```apache
   <Directory /var/www/phpipam>
       Require ip 127.0.0.1
       Require ip ::1
   </Directory>
   ```

6. **Enable PHP security features:**
   - Disable dangerous functions
   - Enable open_basedir restrictions
   - Configure session security

### Network Isolation

If possible, run phpIPAM on a separate management network and configure Proxmox to access it via a dedicated interface.

### Backup Strategy

Regularly backup the phpIPAM database:

```bash
# Create backup
mysqldump -u root phpipam > /root/phpipam-backup-$(date +%Y%m%d).sql

# Restore from backup
mysql -u root phpipam < /root/phpipam-backup-20251203.sql
```

---

## Advanced Configuration

### Using Multiple Sections

phpIPAM sections allow you to organize subnets. You can use different sections for different purposes:

```bash
# Create a new section in phpIPAM
mysql phpipam -e "INSERT INTO sections (name, description) VALUES ('Production', 'Production Networks');"

# Get the section ID
mysql phpipam -e "SELECT id, name FROM sections;"

# Create a new IPAM config pointing to the new section
pvesh create /cluster/sdn/ipams \
  --ipam phpipam-prod \
  --type phpipam \
  --url http://localhost/api/proxmox \
  --token dummy \
  --section 5  # Use the actual section ID
```

### Custom Fields

phpIPAM supports custom fields for subnets and IP addresses. These can be used to store additional metadata like:
- Cost center
- Service owner
- Environment type (dev/staging/prod)
- Compliance requirements

Configure custom fields in the phpIPAM web interface under Administration → Custom Fields.

### Web Interface Access

To access the phpIPAM web interface from outside the Proxmox server:

1. Configure a proper ServerName in Apache
2. Set up DNS or use the Proxmox IP
3. Access via web browser: `http://proxmox-ip/`
4. Default admin credentials: `admin` / `ipamadmin` (change immediately!)

---

## Conclusion

You now have phpIPAM installed and integrated with Proxmox VE SDN. This setup provides:

- ✅ Centralized IP address management
- ✅ Support for overlapping IP ranges across zones
- ✅ Web-based interface for IP tracking
- ✅ Automatic IP registration via SDN integration
- ✅ DHCP range management
- ✅ API access for automation

For more information, consult:
- [phpIPAM Documentation](https://phpipam.net/documents/)
- [Proxmox SDN Documentation](https://pve.proxmox.com/wiki/Software-Defined_Network)
- [phpIPAM API Documentation](https://phpipam.net/api-documentation/)

---

## Appendix: Quick Reference

### Useful Commands

```bash
# View IPAM configuration
pvesh get /cluster/sdn/ipams/phpipam1

# List all zones
pvesh get /cluster/sdn/zones

# List vnets in a zone
pvesh get /cluster/sdn/vnets

# List subnets in a vnet
pvesh get /cluster/sdn/vnets/vnet1/subnets

# Apply SDN configuration
pvesh set /cluster/sdn

# Query phpIPAM API
curl -s http://localhost/api/proxmox/sections/
curl -s http://localhost/api/proxmox/subnets/
curl -s http://localhost/api/proxmox/subnets/7/addresses/

# Check Apache logs
tail -f /var/log/apache2/access.log
tail -f /var/log/apache2/error.log

# Restart services
systemctl restart apache2
systemctl restart pve-cluster
```

### Configuration Files

| File | Purpose |
|------|---------|
| `/var/www/phpipam/config.php` | phpIPAM main configuration |
| `/etc/apache2/sites-available/phpipam.conf` | Apache virtual host |
| `/etc/pve/sdn/ipams.cfg` | Proxmox IPAM configurations |
| `/etc/pve/sdn/zones.cfg` | Proxmox SDN zones |
| `/etc/pve/sdn/vnets.cfg` | Proxmox virtual networks |
| `/etc/pve/sdn/subnets.cfg` | Proxmox subnets |

### API Endpoints

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/proxmox/sections/` | GET | List sections |
| `/api/proxmox/sections/{id}` | GET | Get section details |
| `/api/proxmox/subnets/` | GET | List all subnets |
| `/api/proxmox/subnets/` | POST | Create subnet |
| `/api/proxmox/subnets/{id}` | DELETE | Delete subnet |
| `/api/proxmox/subnets/{id}/addresses/` | GET | List IPs in subnet |
| `/api/proxmox/addresses/` | POST | Create IP |
| `/api/proxmox/addresses/{id}` | DELETE | Delete IP |

---

**Document Version:** 1.0
**Last Updated:** 2025-12-03
**Tested On:** Proxmox VE 9.1.1, phpIPAM 1.6
