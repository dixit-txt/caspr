"""
payment_service.py
Service for handling external payment gateway interactions.
"""

from typing import Dict, Any, Optional
import httpx
from datetime import datetime

from src.config.constants import CASPR_PAYMENT_BASE_URL, CASPR_PAYMENT_API_KEY
from src.config.log_helper import setup_logging

logger = setup_logging(__file__)


class PaymentService:
    """
    Service class for payment gateway operations.
    Encapsulates all interactions with external payment providers (Razorpay).
    """
    
    BASE_URL = CASPR_PAYMENT_BASE_URL
    API_KEY = CASPR_PAYMENT_API_KEY
    
    @classmethod
    async def _make_request(
        cls, 
        method: str, 
        endpoint: str, 
        json_data: Optional[Dict[str, Any]] = None,
        max_retries: int = 3
    ) -> Dict[str, Any]:
        """Helper to make HTTP requests to the payment service with retry logic."""
        url = f"{cls.BASE_URL}{endpoint}"
        headers = {
            "X-API-Key": cls.API_KEY,
            "Content-Type": "application/json"
        }
        
        last_error = None
        for attempt in range(max_retries):
            try:
                async with httpx.AsyncClient(timeout=30.0) as client:
                    if method.upper() == "POST":
                        response = await client.post(url, json=json_data, headers=headers)
                    elif method.upper() == "GET":
                        response = await client.get(url, headers=headers)
                    else:
                        raise ValueError(f"Unsupported HTTP method: {method}")
                        
                    if response.status_code != 200:
                        logger.error(f"Payment service error: {response.status_code} - {response.text}")
                        return {
                            "success": False,
                            "error": "Payment service request failed",
                            "status_code": response.status_code,
                            "details": response.text
                        }
                    
                    response_data = response.json()
                    # Ensure we propagate success status from the service response itself if present
                    if isinstance(response_data, dict) and "success" in response_data and not response_data["success"]:
                        return {
                            "success": False,
                            "error": response_data.get("error", "Payment service returned failure")
                        }
                        
                    # If the service returns just data or success=True wrapper
                    if isinstance(response_data, dict) and "data" in response_data:
                         # If wrapper structure is {success: true, data: {...}}
                         # We return success=True and merge data at top level or keep structure?
                         # Let's standardize on {success: True, data: ...}
                         return response_data
                    
                    # Fallback if raw data returned
                    return {"success": True, "data": response_data}
                    
            except httpx.TimeoutException as e:
                last_error = f"Timeout calling payment service: {str(e)}"
                logger.warning(f"Attempt {attempt + 1}/{max_retries} - {last_error}")
                if attempt < max_retries - 1:
                    continue  # Retry
                logger.error(last_error)
                return {"success": False, "error": "Payment service timeout"}
                
            except (httpx.ConnectError, httpx.NetworkError) as e:
                last_error = f"Network error calling payment service: {str(e)}"
                logger.warning(f"Attempt {attempt + 1}/{max_retries} - {last_error}")
                if attempt < max_retries - 1:
                    continue  # Retry on connection errors
                logger.error(last_error)
                return {"success": False, "error": "Payment service connection failed"}
                
            except Exception as e:
                logger.error(f"Error calling payment service: {str(e)}", exc_info=True)
                return {"success": False, "error": f"Payment service error: {str(e)}"}

    @classmethod
    async def create_topup_order(
        cls,
        idempotency_key: str,
        user_id: str,
        amount: int,
        currency: str,
        country_code: str,
        email: str,
        tokens: int,
    ) -> Dict[str, Any]:
        """
        Create a payment order for wallet top-up.
        
        Args:
            user_id: The ID of the user.
            amount: The amount in USD.
            currency: The currency.
            tokens: The number of tokens to credit.
            country_code: The country code.
            
        Returns:
            Dict containing order details.
        """
        logger.info(f"Creating payment order: user_id={user_id}, amount={amount}")
        
        payload = {
            "idempotency_key": idempotency_key,
            "amount": amount,
            "currency": currency,
            "country": country_code,
            "user_data": {
                "user_id": user_id,
                "email": email,
                "tokens_to_credit": tokens
            }
        }
        
        return await cls._make_request("POST", "/order", payload)

    @classmethod
    async def get_payment_order(
        cls,
        payment_id: Optional[str] = None,
        idempotency_key: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Get payment order status.
        
        Args:
            payment_id: The Razorpay payment ID.
            idempotency_key: The idempotency key used for creation.
            
        Returns:
            Dict containing order status.
        """
        if not payment_id and not idempotency_key:
            return {"success": False, "error": "Either payment_id or idempotency_key is required"}
            
        query_params = []
        if payment_id:
            query_params.append(f"payment_id={payment_id}")
        if idempotency_key:
            query_params.append(f"idempotency_key={idempotency_key}")
            
        endpoint = f"/order?{'&'.join(query_params)}"
        return await cls._make_request("GET", endpoint)

    @classmethod
    async def create_subscription(
        cls,
        idempotency_key: str,
        plan_tier: str,
        plan_duration: str,
        country: str,
        user_data: Dict[str, Any],
        total_count: int = 0,
        start_at: Optional[datetime] = None
    ) -> Dict[str, Any]:
        """
        Create a subscription on the payment gateway.
        
        Args:
            idempotency_key: Unique key to prevent duplicates.
            plan_tier: "PLUS" or "PRO".
            plan_duration: "MONTHLY" or "YEARLY".
            country: ISO country code (e.g. "IN").
            user_data: Dict with 'email', 'user_id', and 'name'.
            total_count: Total billing cycles (0 = unlimited).
            start_at: Optional start datetime.
            
        Returns:
            Dict containing subscription details.
        """
        logger.info(f"Creating subscription: plan={plan_tier}, duration={plan_duration}, country={country}")
        
        payload = {
            "idempotency_key": idempotency_key,
            "plan_tier": plan_tier.upper(),
            "plan_duration": plan_duration.upper(),
            "country": country.upper(),
            "total_count": total_count,
            "user_data": user_data
        }
        
        if start_at:
            payload["start_at"] = start_at.isoformat()
        
        return await cls._make_request("POST", "/subscription", payload)

    @classmethod
    async def get_subscription(
        cls,
        subscription_id: str
    ) -> Dict[str, Any]:
        """
        Get subscription details from payment gateway.
        
        Args:
            subscription_id: The external subscription ID.
            
        Returns:
            Dict containing subscription details.
        """
        if not subscription_id:
            return {"success": False, "error": "subscription_id is required"}
        
        # Strip any trailing spaces from CHAR column padding
        subscription_id = subscription_id.strip()
            
        return await cls._make_request("GET", f"/subscription?subscription_id={subscription_id}")

    @classmethod
    async def cancel_subscription(
        cls,
        subscription_id: str,
        cancel_at_cycle_end: bool = True,
        reason: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Cancel a subscription at the payment gateway.
        
        Args:
            subscription_id: The external subscription ID to cancel.
            cancel_at_cycle_end: If True, cancel at end of billing cycle. If False, cancel immediately.
            reason: Optional reason for cancellation.
            
        Returns:
            Dict containing cancellation status.
        """
        if not subscription_id:
            return {"success": False, "error": "subscription_id is required"}
        
        # Strip any trailing spaces from CHAR column padding
        subscription_id = subscription_id.strip()
        
        logger.info(
            f"Cancelling subscription: subscription_id={subscription_id}, "
            f"cancel_at_cycle_end={cancel_at_cycle_end}, reason={reason}"
        )
        
        payload = {
            "subscription_id": subscription_id,
            "cancel_at_cycle_end": cancel_at_cycle_end
        }
        
        if reason:
            payload["reason"] = reason
        
        return await cls._make_request("POST", "/subscription/cancel", payload)

    @classmethod
    async def update_subscription(
        cls,
        subscription_id: str,
        new_plan_tier: str,
        new_plan_duration: str,
        country: str,
        idempotency_key: str
    ) -> Dict[str, Any]:
        """
        Update a subscription to a new plan at the payment gateway.
        
        The payment service will:
        - Calculate prorated refund/charge based on time elapsed
        - Update the subscription at the gateway (Razorpay/Stripe)
        - Process any refunds or additional charges
        - Send SUBSCRIPTION_UPDATED webhook
        - Send SUBSCRIPTION_PAYMENT_REFUNDED webhook (if refund occurs)
        
        Args:
            subscription_id: The external subscription ID to update.
            new_plan_tier: New tier ("plus" or "pro").
            new_plan_duration: New duration ("monthly" or "yearly").
            country: ISO country code (e.g. "IN", "US").
            idempotency_key: Unique key to prevent duplicate updates.
            
        Returns:
            Dict containing update status and details.
        """
        if not subscription_id:
            return {"success": False, "error": "subscription_id is required"}
        
        # Strip any trailing spaces from CHAR column padding
        subscription_id = subscription_id.strip()
        
        logger.info(
            f"Updating subscription: subscription_id={subscription_id}, "
            f"new_plan={new_plan_tier}_{new_plan_duration}, country={country}"
        )
        
        payload = {
            "subscription_id": subscription_id,
            "new_plan_tier": new_plan_tier.upper(),
            "new_plan_duration": new_plan_duration.upper(),
            "country": country.upper(),
            "idempotency_key": idempotency_key
        }
        
        return await cls._make_request("POST", "/subscription/update", payload)
