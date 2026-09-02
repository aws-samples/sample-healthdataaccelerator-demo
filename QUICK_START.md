# Quick Start Guide

## Deploy the Workshop

### 1. Prerequisites

```bash
# Install AWS CDK globally (pin the version for reproducible installs)
npm install -g aws-cdk@2.1004.0

# Verify installations
aws --version
cdk --version
python3 --version
docker --version
```

### 2. Setup

```bash
# Clone and enter directory
git clone https://github.com/pwbamz/hdademo.git
cd hdademo

# Create virtual environment
python3 -m venv .venv
source .venv/bin/activate

# Install Python dependencies
pip install -r requirements.txt

# Create ECS service-linked roles (first time only)
aws iam create-service-linked-role --aws-service-name ecs.amazonaws.com
aws iam create-service-linked-role --aws-service-name ecs.application-autoscaling.amazonaws.com
```

### 3. Configure

Edit `cdk.json`:

```json
{
  "context": {
    "security_group_ip_range_ipv4": "YOUR.PUBLIC.IP/32"
  }
}
```

Find your IP: `curl ifconfig.me`

### 4. Deploy

```bash
# Bootstrap CDK (first time per account/region)
cdk bootstrap

# Deploy OpenEMR stack
cdk deploy
```

Deployment takes ~40 minutes.

### 5. Access OpenEMR

1. Get the URL from CDK output
2. Login:
   - Username: `admin`
   - Password: In AWS Secrets Manager (secret starting with "Password...")

## Load Sample Data

After deployment, load sample patient data:

```bash
# Connect to database via ECS Exec or port forwarding
# Then run the numbered SQL scripts (01-21)
./install_all_sample_data.sh username database_name
```

## Clean Up

```bash
cdk destroy
```

Manual cleanup:
- AWS Backup vault
- Any S3 buckets with data
