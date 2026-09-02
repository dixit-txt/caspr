"""cloudwatch utils.py:

This file contains the utils for the cloudwatch logs.

"""

import aioboto3
import sys
import time
from typing import List, Dict, Optional
from functools import partial
from src.config.constants import (
    LOG_STREAM,
    LOG_GROUP_NAME,
    CLOUDWATCH_AWS_REGION,
    CLOUDWATCH_AWS_KEY_ID,
    CLOUDWATCH_AWS_SECRET_KEY,
    SUGGESTION_LOG_GROUP_NAME
)


class CloudwatchControls:
    def __init__(self, region_name: str, aws_access_key_id: str, aws_secret_key: str):
        self.region_name = region_name
        self.aws_access_key_id = aws_access_key_id
        self.aws_secret_key = aws_secret_key
        self.session: Optional[aioboto3.Session] = None
        self.cloudwatch_client = None
        self._client_context = None

    async def __is_log_stream_exists(self, log_stream: str, log_group_name: str):
        log_stream_data = await self.cloudwatch_client.describe_log_streams(
            logGroupName=log_group_name,
            logStreamNamePrefix=log_stream
        )
        log_stream_list = log_stream_data.get('logStreams', [])
        for log_stream_dict in log_stream_list:
            if log_stream == log_stream_dict.get('logStreamName'):
                return True
        
        return False
        
    async def __add_log_event_list(
        self, 
        log_event_list: List[Dict], 
        log_stream: str = LOG_STREAM, 
        log_group_name: str = LOG_GROUP_NAME
    ):
        await self.cloudwatch_client.put_log_events(
            logGroupName=log_group_name,
            logStreamName=log_stream,
            logEvents=log_event_list
        )

    async def __create_log_stream(self, log_stream: str = LOG_STREAM, log_group_name: str = LOG_GROUP_NAME):
        exists = await self.__is_log_stream_exists(log_stream=log_stream, log_group_name=log_group_name)
        if exists: 
            return 
        await self.cloudwatch_client.create_log_stream(
            logGroupName=log_group_name,
            logStreamName=log_stream
        )

    async def connect(self):
        """Initialize the CloudWatch client"""
        if self.cloudwatch_client is None:
            self.session = aioboto3.Session()
            self._client_context = self.session.client(
                service_name="logs",
                region_name=self.region_name,
                aws_access_key_id=self.aws_access_key_id,
                aws_secret_access_key=self.aws_secret_key,
            )
            self.cloudwatch_client = await self._client_context.__aenter__()
        
    async def close(self):
        """Close the CloudWatch client"""
        if self._client_context is not None:
            await self._client_context.__aexit__(None, None, None)
            self.cloudwatch_client = None
            self._client_context = None
            self.session = None
  
    async def add_log_messages(
        self, 
        log_messages: List[str], 
        log_stream: str = LOG_STREAM, 
        log_group_name: str = LOG_GROUP_NAME
    ):
        if not log_messages:
            return 
        
        # Ensure client is connected
        if self.cloudwatch_client is None:
            await self.connect()
            
        log_event = [
            {
                'timestamp': int(time.time() * 1000),
                'message': log_message,
            }
            for log_message in log_messages
        ]
        send_logs = partial(
            self.__add_log_event_list, 
            log_event_list=log_event,
            log_stream=log_stream,
            log_group_name=log_group_name
        )
        try:
            await send_logs()
        except Exception as e:
            print(e, file=sys.stderr)
            await self.__create_log_stream(log_stream=log_stream, log_group_name=log_group_name)
            await send_logs()

    async def get_log_messages(self, log_stream: str, log_group_name: str = SUGGESTION_LOG_GROUP_NAME) -> List[Dict]:
        try:
            response = await self.cloudwatch_client.get_log_events(
                logGroupName=log_group_name,
                logStreamName=log_stream,
                startFromHead=True
            )
            return response['events']
        except Exception as e:
            print(e, file=sys.stderr)
            return []


CloudwatchInstance = CloudwatchControls(
    region_name=CLOUDWATCH_AWS_REGION,
    aws_access_key_id=CLOUDWATCH_AWS_KEY_ID,
    aws_secret_key=CLOUDWATCH_AWS_SECRET_KEY
)