"""Model-drafted verifier rules — STAGED, pending human promotion.

Authored by a big open model via rl_gym.gym.rulegen, each gated by the AST sandbox AND by
classifying every pass/fail example in its spec. Loaded ONLY under RLGYM_IAC_GENERATED_RULES
(see rl_gym.iac.scan) — they do NOT affect the default reward until a human promotes them
into scan.RULES.

REVIEW BEFORE PROMOTING: some predicates scope with `resource "..." {[^}]+}`, which stops at
the first `}` and can misbehave on resources with NESTED blocks. Add nested-block cases to the
spec and re-validate (or switch to brace-matching, as sg_open_database_ports / rds_deletion_
protection do) before moving any rule into the always-on set. Auto-generated — do not edit by
hand; re-run scripts/assemble_generated (scratchpad) instead.
"""
import re

def _gen_cloudtrail_encrypted(hcl):
    # Check if there's any aws_cloudtrail resource
    if 'resource "aws_cloudtrail"' not in hcl:
        return "na"
    
    # Find all aws_cloudtrail blocks
    cloudtrail_blocks = []
    pos = 0
    while True:
        start = hcl.find('resource "aws_cloudtrail"', pos)
        if start == -1:
            break
        brace_start = hcl.find("{", start)
        if brace_start == -1:
            break
        # Count braces to find the end of the block
        count = 1
        i = brace_start + 1
        while i < len(hcl) and count > 0:
            if hcl[i] == "{":
                count += 1
            elif hcl[i] == "}":
                count -= 1
            i += 1
        if count == 0:
            cloudtrail_blocks.append(hcl[brace_start+1:i-1])
        pos = i

    # If no aws_cloudtrail block found, return na
    if not cloudtrail_blocks:
        return "na"

    # Check each aws_cloudtrail block for kms_key_id
    for block in cloudtrail_blocks:
        # Look for kms_key_id in the block
        # Match kms_key_id = "value" or kms_key_id = value (without quotes)
        # Using simple string search and re for basic pattern
        if re.search(r'kms_key_id\s*=', block):
            return "pass"
    
    # If no kms_key_id found in any aws_cloudtrail block
    return "fail"

def _gen_cloudtrail_log_validation(hcl):
    if "aws_cloudtrail" not in hcl:
        return "na"
    return "pass" if re.search(r"enable_log_file_validation\s*=\s*true", hcl) else "fail"

def _gen_cloudtrail_multi_region(hcl):
    if "aws_cloudtrail" not in hcl:
        return "na"
    return "pass" if re.search(r"is_multi_region_trail\s*=\s*true", hcl) else "fail"

def _gen_dynamodb_pitr(hcl):
    # Check if there's any aws_dynamodb_table resource
    dynamodb_resources = re.findall(r'resource\s+"aws_dynamodb_table"\s+', hcl)
    if not dynamodb_resources:
        return "na"
    
    # Find each aws_dynamodb_table block
    blocks = re.findall(r'resource\s+"aws_dynamodb_table"\s+"[^"]+"\s+\{[^}]+\}', hcl, re.DOTALL)
    
    for block in blocks:
        # Check if point_in_time_recovery is defined
        pitr_match = re.search(r'point_in_time_recovery\s*\{[^}]*\}', block, re.DOTALL)
        if not pitr_match:
            return "fail"  # point_in_time_recovery block missing
        
        # Check if enabled = true inside the block
        enabled_true = re.search(r'enabled\s*=\s*true', pitr_match.group(0))
        enabled_false = re.search(r'enabled\s*=\s*false', pitr_match.group(0))
        
        if enabled_false:
            return "fail"
        if not enabled_true:
            return "fail"
            
    return "pass"

def _gen_ec2_no_public_ip(hcl):
    if 'aws_instance' not in hcl:
        return "na"
    
    # Find all aws_instance blocks
    instance_blocks = []
    start = 0
    while True:
        start = hcl.find('resource "aws_instance"', start)
        if start == -1:
            break
        # Find the opening brace of the resource
        brace_start = hcl.find('{', start)
        if brace_start == -1:
            break
        # Find the matching closing brace
        depth = 1
        pos = brace_start + 1
        while depth > 0 and pos < len(hcl):
            if hcl[pos] == '{':
                depth += 1
            elif hcl[pos] == '}':
                depth -= 1
            pos += 1
        if depth == 0:
            instance_blocks.append(hcl[brace_start+1:pos-1])
        start = pos

    # Check each aws_instance block for associate_public_ip_address = true
    for block in instance_blocks:
        # Look for associate_public_ip_address = true
        match = re.search(r'associate_public_ip_address\s*=\s*true', block)
        if match:
            return "fail"
    
    return "pass"

def _gen_efs_encrypted(hcl):
    if "aws_efs_file_system" not in hcl:
        return "na"
    return "pass" if re.search(r"encrypted\s*=\s*true", hcl) else "fail"

def _gen_eks_no_public_endpoint(hcl):
    # Check if there's any aws_eks_cluster resource
    if 'resource "aws_eks_cluster"' not in hcl:
        return "na"
    
    # Find all aws_eks_cluster blocks
    cluster_blocks = re.split(r'resource\s+"aws_eks_cluster"\s+"[^"]+"\s+\{', hcl)[1:]
    for block in cluster_blocks:
        # Look for vpc_config block
        vpc_match = re.search(r'vpc_config\s*\{[^}]+\}', block)
        if not vpc_match:
            continue
        vpc_content = vpc_match.group(0)
        # Check if endpoint_public_access is set to true
        public_match = re.search(r'endpoint_public_access\s*=\s*(true|false)', vpc_content)
        if public_match and public_match.group(1) == 'true':
            return "fail"
    
    return "pass"

def _gen_rds_iam_auth(hcl):
    if "aws_db_instance" not in hcl:
        return "na"
    return "pass" if re.search(r"iam_database_authentication_enabled\s*=\s*true", hcl) else "fail"

def _gen_redshift_encrypted(hcl):
    if "aws_redshift_cluster" not in hcl:
        return "na"
    return "pass" if re.search(r"encrypted\s*=\s*true", hcl) else "fail"

def _gen_redshift_public(hcl):
    if "aws_redshift_cluster" not in hcl:
        return "na"
    return "fail" if re.search(r"publicly_accessible\s*=\s*true", hcl) else "pass"

def _gen_sg_open_database_ports(hcl):
    if 'aws_security_group' not in hcl:
        return "na"
    
    import re
    
    sg_blocks = re.findall(r'resource\s+"aws_security_group"[^}]+{[^}]+}', hcl)
    if not sg_blocks:
        return "na"
    
    db_ports = {'3306', '5432', '6379', '27017', '1433', '5439'}
    
    for block in sg_blocks:
        ingress_blocks = re.findall(r'ingress\s*{[^}]+}', block)
        for ig in ingress_blocks:
            from_match = re.search(r'from_port\s*=\s*(\d+)', ig)
            to_match = re.search(r'to_port\s*=\s*(\d+)', ig)
            cidr_match = re.search(r'cidr_blocks\s*=\s*\[([^\]]+)\]', ig)
            
            if not from_match or not to_match or not cidr_match:
                continue
                
            from_port = from_match.group(1)
            to_port = to_match.group(1)
            cidr_content = cidr_match.group(1)
            
            if from_port != to_port:
                continue
                
            if from_port not in db_ports:
                continue
                
            if '"0.0.0.0/0"' in cidr_content or "'0.0.0.0/0'" in cidr_content:
                return "fail"
                
    return "pass"

def _gen_sns_encrypted(hcl):
    import re
    # Check if there's any aws_sns_topic resource
    sns_resources = re.findall(r'resource\s+"aws_sns_topic"\s+', hcl)
    if not sns_resources:
        return "na"
    
    # Find each aws_sns_topic block and check if it has kms_master_key_id
    blocks = re.findall(r'resource\s+"aws_sns_topic"\s+"[^"]+"\s+\{[^}]+\}', hcl, re.DOTALL)
    for block in blocks:
        if not re.search(r'kms_master_key_id\s*=', block):
            return "fail"
    
    return "pass"

def _gen_sqs_encrypted(hcl):
    if "aws_sqs_queue" not in hcl:
        return "na"
    if re.search(r"sqs_managed_sse_enabled\s*=\s*true", hcl):
        return "pass"
    return "pass" if re.search(r"kms_master_key_id\s*=", hcl) else "fail"

def _gen_subnet_no_public_ip(hcl):
    # Check if there's any aws_subnet resource
    subnet_resources = re.findall(r'resource\s+"aws_subnet"\s+"[^"]+"\s+\{[^}]+\}', hcl, re.DOTALL)
    
    if not subnet_resources:
        return "na"
    
    for subnet in subnet_resources:
        # Look for map_public_ip_on_launch setting
        match = re.search(r'map_public_ip_on_launch\s*=\s*(true|false)', subnet)
        if match:
            if match.group(1) == 'true':
                return "fail"
    
    return "pass"


GENERATED_RULES = [
    ('sg_open_database_ports', 'critical', _gen_sg_open_database_ports),
    ('cloudtrail_encrypted', 'high', _gen_cloudtrail_encrypted),
    ('efs_encrypted', 'high', _gen_efs_encrypted),
    ('eks_no_public_endpoint', 'high', _gen_eks_no_public_endpoint),
    ('redshift_encrypted', 'high', _gen_redshift_encrypted),
    ('redshift_public', 'high', _gen_redshift_public),
    ('cloudtrail_log_validation', 'medium', _gen_cloudtrail_log_validation),
    ('cloudtrail_multi_region', 'medium', _gen_cloudtrail_multi_region),
    ('dynamodb_pitr', 'medium', _gen_dynamodb_pitr),
    ('ec2_no_public_ip', 'medium', _gen_ec2_no_public_ip),
    ('sns_encrypted', 'medium', _gen_sns_encrypted),
    ('sqs_encrypted', 'medium', _gen_sqs_encrypted),
    ('subnet_no_public_ip', 'medium', _gen_subnet_no_public_ip),
    ('rds_iam_auth', 'low', _gen_rds_iam_auth),
]

GENERATED_HINTS = {
    'cloudtrail_encrypted': 'set kms_key_id on the aws_cloudtrail',
    'cloudtrail_log_validation': 'set enable_log_file_validation = true on the aws_cloudtrail',
    'cloudtrail_multi_region': 'set is_multi_region_trail = true on the aws_cloudtrail',
    'dynamodb_pitr': 'add point_in_time_recovery { enabled = true } to the aws_dynamodb_table',
    'ec2_no_public_ip': 'remove associate_public_ip_address = true; place the instance behind a NAT/ALB',
    'efs_encrypted': 'set encrypted = true on the aws_efs_file_system',
    'eks_no_public_endpoint': 'set endpoint_public_access = false (or restrict public_access_cidrs) in the eks vpc_config',
    'rds_iam_auth': 'set iam_database_authentication_enabled = true on the aws_db_instance',
    'redshift_encrypted': 'set encrypted = true on the aws_redshift_cluster',
    'redshift_public': 'set publicly_accessible = false on the aws_redshift_cluster',
    'sg_open_database_ports': 'remove 0.0.0.0/0 ingress on the database port; restrict to a private CIDR',
    'sns_encrypted': 'set kms_master_key_id on the aws_sns_topic',
    'sqs_encrypted': 'set sqs_managed_sse_enabled = true (or kms_master_key_id) on the aws_sqs_queue',
    'subnet_no_public_ip': 'set map_public_ip_on_launch = false on the aws_subnet',
}
