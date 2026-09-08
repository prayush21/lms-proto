import json

import boto3

from app.config import AWS_REGION, REPORT_QUEUE, SCORING_QUEUE, SQS_ENDPOINT


def client():
    return boto3.client("sqs", endpoint_url=SQS_ENDPOINT, region_name=AWS_REGION)


def ensure_queues() -> dict[str, str]:
    sqs = client()
    urls: dict[str, str] = {}
    for name in (f"{SCORING_QUEUE}-dlq", f"{REPORT_QUEUE}-dlq"):
        urls[name] = sqs.create_queue(QueueName=name)["QueueUrl"]
    for name in (SCORING_QUEUE, REPORT_QUEUE):
        dlq_name = f"{name}-dlq"
        arn = sqs.get_queue_attributes(
            QueueUrl=urls[dlq_name], AttributeNames=["QueueArn"]
        )["Attributes"]["QueueArn"]
        urls[name] = sqs.create_queue(
            QueueName=name,
            Attributes={
                "VisibilityTimeout": "30",
                "RedrivePolicy": json.dumps(
                    {"deadLetterTargetArn": arn, "maxReceiveCount": "4"}
                ),
            },
        )["QueueUrl"]
    return urls
