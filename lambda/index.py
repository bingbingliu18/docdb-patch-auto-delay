import boto3
import json
import datetime
from io import StringIO
import os

##################### CUSTOMER CONFIGURATIONS #####################
DETECTION_HOURS_INTERVAL = 26 # The number of hours to look ahead for upcoming patches
SNS_ARN = os.environ.get('SNS_TOPIC_ARN')

SNS_REGION = "us-east-1" # The region of the SNS topic
SNS_SUBJECT = "DocumentDB Avoid Patch Notification" # The subject of the SNS notification




##################### Script Constants #####################
ACTION_TYPE_LIST=['system-update']
# Define the absolute maximum date for accurate comparison
MAX_DATE = datetime.datetime.max.replace(tzinfo=datetime.timezone.utc)
# We don't want to send empty notification. By default, don't send unless we modify a cluster's maintenance window    
SHOULD_SEND_NOTIFICATION = False
PATCH_DESCRIPTION_KEYWORDS_TO_NOT_SKIP = ['mandatory', 'security', 'critical']

class UpcomingMaintenanceActionsStillExist(Exception):
    pass

############################################
#                  MAIN                    #
############################################
def lambda_handler(event, context):
    # Get cluster list parameter from SSM parameter store
    try: 
        sam_client = boto3.client('ssm')
        CLUSTER_TARGET_LIST_PAR = sam_client.get_parameter(
        Name='docDB_patch_delay_clusterlist'
        )
        CLUSTER_TARGET_LIST_str = CLUSTER_TARGET_LIST_PAR['Parameter']['Value']
        global CLUSTER_TARGET_LIST 
        CLUSTER_TARGET_LIST = list(CLUSTER_TARGET_LIST_str.split(","))
    except Exception as e:
        print(f"Failed to get SSM parameter store docDB_patch_delay_clusterlist. Error: {e}")
        return ("Failed to get SSM parameter store docDB_patch_delay_clusterlist")
    print(f"Running Script to avoid DocumentDB Cluster Maintenance Actions")

    ec2_client = boto3.client('ec2')
    regions = [region['RegionName'] for region in ec2_client.describe_regions()['Regions']]
    print(f"Running script in EC2 regions: {regions}")
    
    final_notification = StringIO()
    for region in regions:
        region_notification = modify_maintenance_window_for_clusters_in_region(region)
        final_notification.write(region_notification.getvalue())
    
    send_sns_notification(final_notification.getvalue())
    

def modify_maintenance_window_for_clusters_in_region(region):
    """
    In the specified region, do the following:
        1. Find any clusters with a maintenance action set to apply with the next 'detection_hours' interval
        2. Modify the Cluster Maintenance Window of these clusters so the patch is re-scheduled for ~6 days in the future
        3. Publish the list of clusters modified (or any failures) to the provided SNS topic
    """
    # Initialize the notification string
    region_notification = StringIO()
    region_notification.write("**********************************************\n")
    region_notification.write(f"Region: {region}\n")
    
    docdb_client = boto3.client('docdb', region_name=region)
    
    # Execute core logic for finding and modifying clusters to avoid patches
    clusters_modified_success, clusters_failed_to_modify = find_and_modify_clusters_mw_with_upcoming_maintenance_actions(docdb_client, detection_hours=DETECTION_HOURS_INTERVAL)
    
    # Verify no actions remain
    # If pending actions DO remain, update the list of fail to modify
    if not clusters_failed_to_modify:
        clusters_failed_to_modify = verify_no_upcoming_maintenance_actions_return_remaining(docdb_client, DETECTION_HOURS_INTERVAL)
    
    if not clusters_failed_to_modify and not clusters_modified_success:
        region_notification.write(f"No clusters with upcoming maintenance actions in region {region}\n")
        region_notification.write("**********************************************\n\n")
        return region_notification
    
    # We have made a modification (or we need to and failed), so send a notification    
    global SHOULD_SEND_NOTIFICATION
    SHOULD_SEND_NOTIFICATION = True
    
    # Construct the rest of the notification
    log_successful_cluster_modification(clusters_modified_success, region_notification)
    
    if clusters_failed_to_modify:
        log_failed_cluster_modification(clusters_failed_to_modify, region_notification)
    
    region_notification.write(f"Total number of clusters modified in {region}: {len(clusters_modified_success)}\n")
    region_notification.write("**********************************************\n\n")
    
    return region_notification
    

############################################
#           Core Logic Functions           #
############################################
def find_and_modify_clusters_mw_with_upcoming_maintenance_actions(docdb_client, detection_hours):
    """
    Find clusters with upcoming maintenance actions and modify the maintenance window
    """
    target_clusters_to_modify = fetch_clusters_with_upcoming_patches(docdb_client, detection_hours=detection_hours)
    if not target_clusters_to_modify:
        print("No clusters found with upcoming maintenance actions")
        return [], []
    print("Found {} clusters with upcoming maintenance actions".format(len(target_clusters_to_modify)))
    clusters_successfully_modified, clusters_failed_to_modify = modify_maintenance_window_for_clusters(docdb_client, target_clusters_to_modify)
    
    return clusters_successfully_modified, clusters_failed_to_modify
    
def fetch_clusters_with_upcoming_patches(docdb_client, detection_hours):
    """
    Fetch all of the clusters that have a pending maintenance action that is set to apply within 
    the next 'detection_hours' interval
    """
    target_clusters_to_modify = []
    pending_maintenance_actions = docdb_client.describe_pending_maintenance_actions()
    if not pending_maintenance_actions['PendingMaintenanceActions'] or len(pending_maintenance_actions['PendingMaintenanceActions']) < 1:
        print("No pending maintenance actions found")
        return target_clusters_to_modify
    
    for pending_maintenance_action in pending_maintenance_actions['PendingMaintenanceActions']:
        # ONLY execute this for clusters in the CLUSTER_TARGET_LIST
        cluster_id = pending_maintenance_action["ResourceIdentifier"].split(':')[-1] # Cluster name is last part of AWS resource ARN
        if cluster_id not in CLUSTER_TARGET_LIST:
            print("not in cluster list")
            continue
        if not pending_action_is_docdb_cluster_engine_patch(docdb_client, pending_maintenance_action):
            continue
        
        # If any of the keywords are included in the description, do NOT skip the patch
        if any([skip_keyword in pending_maintenance_action['PendingMaintenanceActionDetails'][0]['Description'].lower() for skip_keyword in PATCH_DESCRIPTION_KEYWORDS_TO_NOT_SKIP]):
            continue
        nearest_apply_date = get_nearest_apply_date_for_pending_maintenance_action(pending_maintenance_action) 
        if nearest_apply_date == MAX_DATE:
            continue
        
        if ((nearest_apply_date - datetime.datetime.now(datetime.timezone.utc)) > datetime.timedelta(hours=detection_hours)):
            continue
        
        # Now, we know the apply date is within the next 'detection_hours' interval, so we add it to the list to return
        target_clusters_to_modify.append({'cluster_arn': pending_maintenance_action['ResourceIdentifier'], 'apply_date': nearest_apply_date})
    return target_clusters_to_modify

def modify_maintenance_window_for_clusters(docdb_client, target_clusters_to_modify):
    """
    Modify the maintenance window for the clusters that have been identified to have 
    pending maintenance actions within the 'detection interval' provided earlier
    """
    modified_clusters_success = []
    modified_clusters_failure = []
    for target_cluster in target_clusters_to_modify:
        print(f"Modifying maintenance window for cluster {target_cluster['cluster_arn']}")
        # go back one day in the past to reset the maintenance apply date for approx 1 week in the future
        new_target_apply_date = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=1)
        new_maintenance_window = f"{new_target_apply_date.strftime('%a:%H:%M')}-{(new_target_apply_date + datetime.timedelta(minutes=30)).strftime('%a:%H:%M')}"
        try:
            response = docdb_client.modify_db_cluster(
                DBClusterIdentifier=target_cluster['cluster_arn'].split(':')[-1], # Cluster name is last part of AWS resource ARN
                PreferredMaintenanceWindow=new_maintenance_window,
                ApplyImmediately=True
            )
            print(f"Modify DB Cluster response: {json.dumps(response, indent=4, default=str)}")
            modified_clusters_success.append({'cluster_arn': target_cluster['cluster_arn'], 'previous_apply_date': target_cluster['apply_date'], 'new_apply_date': get_new_apply_date(docdb_client, target_cluster['cluster_arn'])})
            print(f"Successfully modified maintenance window for cluster {target_cluster['cluster_arn']}. New maintenance window: {new_maintenance_window}")
        except Exception as e:
            print(f"Failed to modify maintenance window for cluster {target_cluster['cluster_arn']}. Error: {e}")
            modified_clusters_failure.append({'cluster_arn': target_cluster['cluster_arn'], 'previous_apply_date': target_cluster['apply_date'], 'new_apply_date': new_target_apply_date})
    return modified_clusters_success, modified_clusters_failure

############################################
#         Verification Utilities           #
############################################
def verify_no_upcoming_pending_maintenance_actions(docdb_client, detection_hours):
    """
    Verify that there are no upcoming pending maintenance actions
    """
    clusters_to_modify = fetch_clusters_with_upcoming_patches(docdb_client, detection_hours=detection_hours)
    if not clusters_to_modify:
        print("Verification SUCCESS")
        return
    raise UpcomingMaintenanceActionsStillExist("Found {} clusters with upcoming maintenance actions".format(len(clusters_to_modify)))

def verify_no_upcoming_maintenance_actions_return_remaining(docdb_client, detection_hours):
    """
    Run the verification. If any pending mainteance actions remain, return them
    """
    try:
        verify_no_upcoming_pending_maintenance_actions(docdb_client, detection_hours=detection_hours)
    except UpcomingMaintenanceActionsStillExist as e:
        # Log the verification failure
        print(e)
        return fetch_clusters_with_upcoming_patches(docdb_client, detection_hours=detection_hours)

############################################
#               Log Utilities              #
############################################
def log_successful_cluster_modification(clusters_modified_success, notification):
    """
    Helper function for logging the clusters modified and outputting these clusters to the SNS notification message body
    """
    notification.write("Successfully modified the maintenance window of the following clusters in order to avoid an upcoming maintenance action: \n")
    for cluster_modified in clusters_modified_success:
        print(f"\t - {json.dumps(cluster_modified, default=str)}")
        notification.write(f"\t- ClusterARN: {cluster_modified['cluster_arn']}\n")
        notification.write(f"\t\t* OLD apply_date =>    {cluster_modified['previous_apply_date']}\n")
        notification.write(f"\t\t* NEW apply_date =>    {cluster_modified['new_apply_date']}\n")
    
def log_failed_cluster_modification(clusters_failed_to_modify, notification):
    """
    Helper function for logging the clusters we FAILED to modify and outputting these clusters to the SNS notification message body
    """
    notification.write("Failed to modify the maintenance window of the following clusters \n")
    for cluster_failed in clusters_failed_to_modify:
        print(f"\t - {json.dumps(cluster_failed, default=str)}")
        notification.write(f"\t- ClusterARN: {cluster_failed['cluster_arn']}\n")
        notification.write(f"\t\t* apply_date =>     {cluster_failed.get('previous_apply_date', 'N/A')}\n")

############################################
#              Date Utilities              #
############################################
def get_nearest_apply_date_for_pending_maintenance_action(pending_maintenance_action):
    # Get the nearest apply date for the cluster
    nearest_apply_date = min([
        pending_maintenance_action['PendingMaintenanceActionDetails'][0].get('CurrentApplyDate', MAX_DATE),
        pending_maintenance_action['PendingMaintenanceActionDetails'][0].get('ForcedApplyDate', MAX_DATE),
    ])
    return nearest_apply_date

def get_new_apply_date(docdb_client, cluster_arn):
    """
    Get the new apply date for the cluster
    """
    cluster_pending_maintenance_actions = docdb_client.describe_pending_maintenance_actions(ResourceIdentifier=cluster_arn)
    assert len(cluster_pending_maintenance_actions["PendingMaintenanceActions"]) < 2, f"Expected only one (or zero) pending maintenance action for cluster {cluster_arn}"
    
    return get_nearest_apply_date_for_pending_maintenance_action(cluster_pending_maintenance_actions["PendingMaintenanceActions"][0])

############################################
#               SNS Utilities              #
############################################
def send_sns_notification(notification):
    if not SHOULD_SEND_NOTIFICATION:
        print(f"Skipping SNS notification. No modification to clusters detected")
        return
    
    sns_client = boto3.client('sns', region_name=SNS_REGION)
    sns_response = sns_client.publish(
        TargetArn = SNS_ARN,
        Subject = SNS_SUBJECT,
        Message = json.dumps({'default': notification}),
        MessageStructure = 'json'
    )
    print(f"Published SNS notification => {json.dumps(sns_response, indent=4, default=str)}")


############################################
#            General Utilities             #
############################################
def pending_action_is_docdb_cluster_engine_patch(docdb_client, pending_action):
    """
    For a pending maintenance action to be a DocumentDB cluster engine patch, ALL of the following MUST be true:
    
    1. The ARN of the pending maintenance action is for a 'cluster'
        * Ex. arn = "arn:aws:rds:us-east-1:000123456789:cluster:test-cluster-1"
    2. The 'action' of a pending maintenance action must be type 'system-update'
        * Ex. action = {..., "Action": "system-update", ...}
    3. The "Engine" of the DB cluster must be "docdb"
    """
    # 1
    pending_action_resource_arn = pending_action['ResourceIdentifier']
    if pending_action_resource_arn.split(":")[5] != "cluster":
        return False
    # 2
    action_type = pending_action['PendingMaintenanceActionDetails'][0].get('Action')
    if action_type != "system-update":
        return False
    # 3 
    cluster_identifier = pending_action_resource_arn.split(":")[6]
    describe_db_cluster_response = docdb_client.describe_db_clusters(DBClusterIdentifier=cluster_identifier)
    if describe_db_cluster_response["DBClusters"][0]["Engine"] != "docdb":
        return False
    return True