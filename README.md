# docDB Patch Auto Delay

## Introduction
Periodically, Amazon DocumentDB, perform maintenance on resources. Maintenance most often involves updates to the DB cluster's underlying hardware, operating system (OS), or database engine version. It is necessary to apply database patches in a timely manner and update the OS or database engine version to better protect your cloud database. 

However, in some scenarios, you may want to delay the application of docDB engine patches while you have numerous docDB clusters and it would be convenient to automate delaying the application of patches to multiple clusters using scripts. Therefore, here we provide a docDB patch auto delay script.

## Solution components: To accomplish patch auto delay script, we use the following services:
•	AWS Lambda – Deploy a Lambda function to delay patches of Amazon DocumentDB  
•	Amazon EventBridge – Schedule the Lambda function run one time a day  
•	Amazon SNS – Send email notification for patch delay of Amazon DocumentDB  


## Create SSM Parameter Store (name=docDB_patch_delay_clusterlist) of docDB clusters list for docDB Patch Delay.
for example, the cluster list is:"abc,bcd,cde,def", please change the cluster list to your cluster list.

aws ssm put-parameter \
    --name docDB_patch_delay_clusterlist \
    --value "your cluster list" \
    --type StringList

## Inputs
The following are the inputs to the SAM template:
- EmailAddress for receiving email



## Build
To validate the SAM template run:

```
sam validate
```

To build the SAM template run:

```
sam build
```

or 

```
make build
```

## Deploy
To deploy, run

```
sam deploy --capabilities CAPABILITY_NAMED_IAM --guided
```

or 

```
make sam
```
## Confirm Email subscription
Login in your email system, please confirm your email subscription for the SNS topic "DocumentDB Avoid Patch Notification" by click the link "Confirm subscription" in your email.

## Adjust the schedule time for running the Lambda function at a suitable time. 
please pay attention: the schduled lambda run time should not overlap with the backup window time of the docDB clusters， otherwise you will not be able to modify maintenance window successfully!!!   
Please visit Eventbridge console Rules and edit the rule.

==============================================

Copyright 2020 Amazon.com, Inc. or its affiliates. All Rights Reserved.

SPDX-License-Identifier: MIT-0

