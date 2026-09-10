"""redis_utils.py: Utility functions for Redis operations"""

import json
from datetime import datetime
from functools import lru_cache

import redis.asyncio as aioredis

from app.core.constants import REDIS_DB, REDIS_HOST, REDIS_PASSWORD, REDIS_PORT, REDIS_TTL
from app.core.logging import setup_logging

logger = setup_logging(__file__)


class RedisManager:
    """Redis manager for chat operations"""

    def __init__(self, host=None, port=None, db=None, decode_responses=True):
        """Initialize Redis connection

        Args:
            host: Redis host
            port: Redis port
            db: Redis database number
            decode_responses: Whether to decode responses to Python strings
        """
        # Use values from constants.py if not provided
        redis_host = host or REDIS_HOST
        redis_port = port or REDIS_PORT
        redis_db = db if db is not None else REDIS_DB

        # Strip http:// or https:// prefix if present (Redis doesn't use HTTP protocol)
        if redis_host:
            redis_host = redis_host.replace("http://", "").replace("https://", "")

        redis_kwargs = {
            "host": redis_host,
            "port": redis_port,
            "db": redis_db,
            "decode_responses": decode_responses,
        }
        if REDIS_PASSWORD is not None:
            redis_kwargs["password"] = REDIS_PASSWORD

        self.redis_client = aioredis.Redis(**redis_kwargs)
        logger.info(f"Redis manager initialized with host: {redis_host}, port: {redis_port}")

    async def create_chat(self, chat_data, ttl=REDIS_TTL):
        """Create a new chat in Redis

        Args:
            chat_data: Chat data to store

        Returns:
            dict: Response with success status
        """
        logger.info(
            f"Creating chat with ID: {chat_data.get('chat_id')}, title: {chat_data.get('chat_title')}, message_count: {len(chat_data.get('chat_messages'))}"
        )

        required_fields = [
            "chat_id",
            "user_id",
            "chat_title",
            "chat_messages",
            "created_at",
            "updated_at",
        ]

        for field in required_fields:
            if field not in chat_data:
                logger.error(f"Missing required field: {field}")
                return {"success": False, "error": f"Missing required field: {field}"}

        try:
            if await self.redis_client.exists(f"temp_chat:{chat_data.get('chat_id')}"):
                logger.error(f"Chat with ID: {chat_data.get('chat_id')} already exists")
                return {"success": False, "error": "Chat already exists"}

            chat_data["created_at"] = chat_data["created_at"].isoformat()
            chat_data["updated_at"] = chat_data["updated_at"].isoformat()
            chat_data["chat_messages"] = json.dumps(chat_data.get("chat_messages", []))

            await self.redis_client.hset(f"temp_chat:{chat_data.get('chat_id')}", mapping=chat_data)
            logger.info(f"Chat created successfully with ID: {chat_data.get('chat_id')}")

            await self.redis_client.expire(f"temp_chat:{chat_data.get('chat_id')}", ttl)

            return {"success": True, "message": "Chat created successfully"}

        except Exception as e:
            logger.error(f"Error while creating chat: {e}", exc_info=True)
            return {"success": False, "error": "Error while creating chat"}

    async def get_chat(self, chat_id, ttl=REDIS_TTL):
        """Get chat data by ID

        Args:
            chat_id: Chat ID to retrieve

        Returns:
            dict: Chat data or error response
        """
        logger.info(f"Getting chat with ID: {chat_id}")

        try:
            if not chat_id:
                logger.error("Missing required chat_id field")
                return {"success": False, "error": "Missing required fields"}

            exists = await self.redis_client.exists(f"temp_chat:{chat_id}")
            if not exists:
                logger.error(f"Chat with ID: {chat_id} not found")
                return {"success": False, "error": "Chat not found"}

            chat_data = await self.redis_client.hgetall(f"temp_chat:{chat_id}")

            if not chat_data:
                logger.error(f"Chat with ID: {chat_id} not found")
                return {"success": False, "error": "Chat not found"}

            # Reset TTL on access to keep active chats alive
            await self.redis_client.expire(f"temp_chat:{chat_id}", ttl)
            chat_data["chat_messages"] = json.loads(chat_data.get("chat_messages", []))
            chat_data["created_at"] = (
                datetime.fromisoformat(chat_data["created_at"]) if chat_data["created_at"] else ""
            )
            chat_data["updated_at"] = (
                datetime.fromisoformat(chat_data["updated_at"]) if chat_data["updated_at"] else ""
            )

            # Return the full chat data
            logger.info(
                f"Chat with ID: {chat_id} found in redis with {len(chat_data['chat_messages']) if chat_data.get('chat_messages') else 0} messages"
            )
            return {"success": True, "chat_data": chat_data}

        except Exception as e:
            logger.error(f"Failed to get chat with ID: {chat_id}: {e!s}", exc_info=True)
            return {"success": False, "error": str(e)}

    async def update_chat(self, chat_data, ttl=REDIS_TTL):
        """Update chat fields

        Args:
            chat_id: Chat ID to update
            chat_messages: New chat messages
            updated_at: Last update timestamp

        Returns:
            dict: Response with success status
        """
        logger.info(f"Updating chat with ID: {chat_data.get('chat_id')}")

        required_fields = ["chat_id", "chat_messages", "updated_at"]

        for field in required_fields:
            if field not in chat_data:
                logger.error(f"Missing required field: {field}")
                return {"success": False, "error": f"Missing required field: {field}"}

        try:
            # Check if chat exists
            exists = await self.redis_client.exists(f"temp_chat:{chat_data.get('chat_id')}")
            if not exists:
                logger.error(f"Chat with ID: {chat_data.get('chat_id')} not found")
                return {"success": False, "error": "Chat not found"}

            # Prepare update data
            update_data = {
                "updated_at": chat_data.get("updated_at").isoformat(),
                "chat_messages": json.dumps(chat_data.get("chat_messages")),
            }

            await self.redis_client.hset(
                f"temp_chat:{chat_data.get('chat_id')}", mapping=update_data
            )
            logger.info(f"Chat with ID: {chat_data.get('chat_id')} updated successfully")

            # Reset TTL on update
            await self.redis_client.expire(f"temp_chat:{chat_data.get('chat_id')}", ttl)

            return {"success": True, "message": "Chat updated successfully"}

        except Exception as e:
            logger.error(
                f"Failed to update chat with ID: {chat_data.get('chat_id')}: {e!s}",
                exc_info=True,
            )
            return {"success": False, "error": str(e)}

    # async def get_user_chats(self, user_id, ttl=REDIS_TTL):
    #     """Return a list of full chat hash records for the given user_id.

    #     Each record includes user_id, chat_title, chat_messages, created_at, updated_at, etc.
    #     """

    #     logger.info(f"Getting all chats for user with ID: {user_id}")

    #     try:
    #         if not user_id:
    #             logger.error("Missing required user_id field")
    #             return {"success": False, "error": "Missing required fields"}

    #         cursor = 0
    #         user_chats = []

    #         while True:
    #             cursor, keys = await self.redis_client.scan(cursor=cursor, match="chat:*", count=100)
    #             for key in keys:
    #                 stored_user_id = await self.redis_client.hget(key, "user_id")
    #                 if stored_user_id == user_id:
    #                     chat_data = await self.redis_client.hgetall(key)
    #                     chat_data["chat_messages"] = json.loads(chat_data["chat_messages"])
    #                     chat_data['created_at'] = datetime.fromisoformat(chat_data['created_at']) if chat_data['created_at'] else ""
    #                     chat_data['updated_at'] = datetime.fromisoformat(chat_data['updated_at']) if chat_data['updated_at'] else ""

    #                     # Add Redis key as chat_id
    #                     chat_data["chat_id"] = key.split('chat:')[-1]

    #                     user_chats.append(chat_data)

    #                     # Refresh TTL on matched key
    #                     await self.redis_client.expire(key, ttl)

    #             if cursor == 0:
    #                 break

    #         logger.info(f"Found {len(user_chats) if user_chats else 0} chats for user_id: {user_id}")
    #         return {"success": True, "chats": user_chats}

    #     except Exception as e:
    #         logger.error(f"Failed to get chats for user_id={user_id}: {str(e)}", exc_info=True)
    #         return {"success": False, "error": str(e)}

    async def check_chat_exists(self, chat_id, ttl=REDIS_TTL):
        """Check if a chat exists in Redis

        Args:
            chat_id: Chat ID to check
        """
        logger.info(f"Checking if chat with ID: {chat_id} exists")

        try:
            if not chat_id:
                logger.error("Missing required chat_id field")
                return {"success": False, "error": "Missing required fields"}

            exists = await self.redis_client.exists(f"temp_chat:{chat_id}")
            if not exists:
                logger.warning(f"Chat with ID: {chat_id} not found in redis")
                return {"success": True, "exists": False}

            await self.redis_client.expire(f"temp_chat:{chat_id}", ttl)
            return {"success": True, "exists": True}

        except Exception as e:
            logger.error(f"Failed to check if chat with ID: {chat_id} exists: {e!s}", exc_info=True)
            return {"success": False, "error": str(e)}

    async def delete_chat(self, chat_id):
        """Delete a chat

        Args:
            chat_id: Chat ID to delete

        Returns:
            dict: Response with success status
        """
        logger.info(f"Deleting chat with ID: {chat_id}")

        try:
            if not chat_id:
                logger.error("Missing required chat_id field")
                return {"success": False, "error": "Missing required fields"}

            exists = await self.redis_client.exists(f"temp_chat:{chat_id}")
            if not exists:
                logger.error(f"Chat with ID: {chat_id} not found in redis")
                return {"success": False, "error": "Chat not found in redis"}

            await self.redis_client.delete(f"temp_chat:{chat_id}")
            logger.info(f"Chat with ID: {chat_id} deleted successfully")
            return {"success": True, "message": "Chat deleted successfully"}

        except Exception as e:
            logger.error(f"Failed to delete chat with ID: {chat_id}: {e!s}", exc_info=True)
            return {"success": False, "error": str(e)}

    # async def update_user_in_chat(self, chat_id, user_id, ttl=REDIS_TTL):
    #     """Update the user_id in the chat

    #     Args:
    #         chat_id: Chat ID to update
    #         user_id: User ID to update
    #     """
    #     logger.info(f"Updating user in chat with ID: {chat_id} to user_id: {user_id}")

    #     try:
    #         if not chat_id or not user_id:
    #             logger.error("Missing required chat_id or user_id field")
    #             return {"success": False, "error": "Missing required fields"}

    #         await self.redis_client.hset(f'chat:{chat_id}', mapping={'user_id': user_id})
    #         await self.redis_client.expire(f'chat:{chat_id}', ttl)
    #         logger.info(f"User with ID: {user_id} updated in chat with ID: {chat_id}")
    #         return {"success": True, "message": "User updated in chat successfully"}

    #     except Exception as e:
    #         logger.error(f"Failed to update user in chat with ID: {chat_id} to user_id: {user_id}: {str(e)}", exc_info=True)
    #         return {"success": False, "error": str(e)}

    # async def create_user(self, user_data, ttl=REDIS_TTL):
    #     """Create a new user in Redis

    #     Args:
    #         user_data: User data to store
    #         ttl: Time to live for the user data

    #     Returns:
    #         dict: Response with success status
    #     """
    #     logger.info(f"Creating user with data: {user_data.get('user_id')}")

    #     if not user_data:
    #         logger.error("Missing required user_data field")
    #         return {"success": False, "error": "Missing required fields"}

    #     required_fields = [
    #         'user_id',
    #         'email',
    #         'phone',
    #         'created_at',
    #         'user_name',
    #         'phone_country_code',
    #         'password_hash',
    #         'is_google_verified'
    #     ]

    #     for field in required_fields:
    #         if field not in user_data:
    #             logger.error(f"Missing required field: {field}")
    #             return {"success": False, "error": f"Missing required field: {field}"}

    #     try:
    #         if await self.redis_client.exists(f"user:{user_data.get('user_id')}"):
    #             logger.error(f"User with ID: {user_data.get('user_id')} already exists")
    #             return {"success": False, "error": "User already exists"}

    #         user_data['is_google_verified'] = int(user_data.get('is_google_verified', 0))
    #         user_data['created_at'] = user_data['created_at'].isoformat() if user_data['created_at'] else datetime.now(timezone.utc).isoformat()

    #         cache_key = f"user:{user_data.get('user_id')}"
    #         await self.redis_client.hset(cache_key, mapping=user_data)
    #         await self.redis_client.expire(cache_key, ttl)
    #         logger.info(f"User with ID: {user_data.get('user_id')} created successfully")

    #         return {"success": True, "message": "User created successfully"}

    #     except Exception as e:
    #         logger.error(f"Failed to create user with ID: {user_data.get('user_id')}: {str(e)}", exc_info=True)
    #         return {"success": False, "error": str(e)}

    # async def check_user_exists(self, user_id, ttl=REDIS_TTL):
    #     """Check if a user exists in Redis

    #     Args:
    #         user_id: User ID to check
    #         ttl: Time to live for the user data

    #     Returns:
    #         dict: Response with success status
    #     """
    #     logger.info(f"Checking if user with ID: {user_id} exists")

    #     try:
    #         if not user_id:
    #             logger.error("Missing required user_id field")
    #             return {"success": False, "error": "Missing required fields"}

    #         exists = await self.redis_client.exists(f"user:{user_id}")
    #         if not exists:
    #             logger.warning(f"User with ID: {user_id} not found in redis")
    #             return {"success": True, "exists": False}

    #         await self.redis_client.expire(f"user:{user_id}", ttl)
    #         logger.info(f"User with ID: {user_id} found in redis")

    #         return {"success": True, "exists": True}

    #     except Exception as e:
    #         logger.error(f"Failed to check if user with ID: {user_id} exists: {str(e)}", exc_info=True)
    #         return {"success": False, "error": str(e)}

    # async def get_user(self, user_id, ttl=REDIS_TTL):
    #     """Get user data by ID

    #     Args:
    #         user_id: User ID to retrieve
    #         ttl: Time to live for the user data

    #     Returns:
    #         dict: User data or error response
    #     """
    #     logger.info(f"Getting user with ID: {user_id}")

    #     try:
    #         if not user_id:
    #             logger.error("Missing required user_id field")
    #             return {"success": False, "error": "Missing required fields"}

    #         exists = await self.redis_client.exists(f"user:{user_id}")
    #         if not exists:
    #             logger.error(f"User with ID: {user_id} not found")
    #             return {"success": False, "error": "User not found"}

    #         user_data = await self.redis_client.hgetall(f"user:{user_id}")
    #         user_data['created_at'] = datetime.fromisoformat(user_data.get('created_at'))
    #         user_data['is_google_verified'] = bool(user_data.get('is_google_verified', 0))

    #         if not user_data:
    #             logger.error(f"User with ID: {user_id} not found")
    #             return {"success": False, "error": "User not found"}

    #         await self.redis_client.expire(f"user:{user_id}", ttl)
    #         return {"success": True, "user_data": user_data}

    #     except Exception as e:
    #         logger.error(f"Failed to get user with ID: {user_id}: {str(e)}", exc_info=True)
    #         return {"success": False, "error": str(e)}

    # TODO: Add update_user: for forgot password updated

    # async def insert_report(self, report_data, ttl=REDIS_TTL):
    #     """Insert a report into Redis

    #     Args:
    #         report_data: Report data to store
    #         ttl: Time to live for the report data
    #     """
    #     logger.info(f"Inserting report with ID: {report_data.get('report_id')}")

    #     required_fields = [
    #         'report_id',
    #         'chat_id',
    #         'created_at',
    #         'source_documents',
    #         's3_uri'
    #     ]

    #     for field in required_fields:
    #         if field not in report_data:
    #             logger.error(f"Missing required field: {field}")
    #             return {"success": False, "error": f"Missing required field: {field}"}

    #     try:

    #         exists = await self.redis_client.exists(f"report:{report_data.get('report_id')}")
    #         if exists:
    #             logger.error(f"Report with ID: {report_data.get('report_id')} already exists")
    #             return {"success": False, "error": "Report already exists"}

    #         report_data['created_at'] = report_data['created_at'].isoformat() if report_data['created_at'] else datetime.now(timezone.utc).isoformat()
    #         report_data['source_documents'] = json.dumps(report_data.get('source_documents', []))
    #         report_data['s3_uri'] = json.dumps(report_data.get('s3_uri', []))
    #         cache_key = f"report:{report_data.get('report_id')}"
    #         await self.redis_client.hset(cache_key, mapping=report_data)
    #         await self.redis_client.expire(cache_key, ttl)

    #         return {"success": True, "message": "Report created successfully"}

    #     except Exception as e:
    #         logger.error(f"Failed to insert report with ID: {report_data.get('report_id')}: {str(e)}", exc_info=True)
    #         return {"success": False, "error": str(e)}

    # async def check_report_exists(self, report_id):
    #     """Check if a report exists in Redis

    #     Args:
    #         report_id: Report ID to check
    #     """
    #     logger.info(f"Checking if report with ID: {report_id} exists")

    #     try:
    #         if not report_id:
    #             logger.error("Missing required report_id field")
    #             return {"success": False, "error": "Missing required fields"}

    #         exists = await self.redis_client.exists(f"report:{report_id}")
    #         if not exists:
    #             logger.warning(f"Report with ID: {report_id} not found in redis")
    #             return {"success": True, "exists": False}

    #         return {"success": True, "exists": True}

    #     except Exception as e:
    #         logger.error(f"Failed to check if report with ID: {report_id} exists: {str(e)}", exc_info=True)
    #         return {"success": False, "error": str(e)}

    # async def get_chat_report(self, chat_id, ttl=REDIS_TTL):
    #     """Get all reports for a given chat_id across Redis hashes.

    #     Args:
    #         chat_id: Chat ID to retrieve reports for
    #         ttl: Optional TTL to refresh on each matching key

    #     Returns:
    #         dict with success flag and list of matching reports
    #     """
    #     logger.info(f"Fetching reports for chat_id: {chat_id}")

    #     try:
    #         if not chat_id:
    #             logger.error("Missing required chat_id field")
    #             return {"success": False, "error": "Missing required fields"}

    #         cursor = 0
    #         found_reports = []

    #         while True:
    #             cursor, keys = await self.redis_client.scan(cursor=cursor, match="report:*", count=100)

    #             for key in keys:
    #                 stored_chat_id = await self.redis_client.hget(key, "chat_id")
    #                 if stored_chat_id == chat_id:
    #                     s3_uri_raw = await self.redis_client.hget(key, "s3_uri")
    #                     source_docs_raw = await self.redis_client.hget(key, "source_documents")
    #                     created_at = await self.redis_client.hget(key, "created_at")
    #                     created_at = datetime.fromisoformat(created_at) if created_at else None
    #                     s3_uri = json.loads(s3_uri_raw) if s3_uri_raw else None
    #                     source_documents = json.loads(source_docs_raw) if source_docs_raw else None

    #                     # Extract report_id from key: "report:{id}"
    #                     report_id = key.split("report:")[-1]

    #                     found_reports.append({
    #                         "report_id": report_id,
    #                         "s3_uri": s3_uri,
    #                         "created_at": created_at,
    #                         "source_documents": source_documents
    #                     })

    #                     # Refresh TTL
    #                     await self.redis_client.expire(key, ttl)

    #             if cursor == 0:
    #                 break

    #         logger.info(f"Found {len(found_reports) if found_reports else 0} reports for chat_id: {chat_id}")
    #         return {"success": True, "reports": found_reports}

    #     except Exception as e:
    #         logger.error(f"Error retrieving reports for chat_id {chat_id}: {str(e)}", exc_info=True)
    #         return {"success": False, "error": str(e)}


@lru_cache(maxsize=1)
def get_redis_instance(host=REDIS_HOST, port=REDIS_PORT, db=REDIS_DB):
    """Get a singleton instance of RedisManager"""
    try:
        return RedisManager(host=host, port=port, db=db)
    except Exception as e:
        logger.error(f"Failed to get Redis instance: {e!s}", exc_info=True)
        raise e
