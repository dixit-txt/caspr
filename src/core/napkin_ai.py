import requests
import json
import time
from src.config.constants import NAPKIN_API_TOKEN

def create_request(content: str, 
    context: str, 
    format: str = "svg",
    visual_queries: list[str] = None, 
    number_of_visuals: int = 1, 
    transparent_background: bool = False, 
    color_mode: str = "light", 
    width: int = 1200, 
    height: int = 800, 
    orientation: str = "square", 
    text_extraction_mode: str = "preserve", 
    sort_strategy: str = "relevance"):
    try:
        url = "https://api.napkin.ai/v1/visual"

        payload = json.dumps({
            "format": format, # this should be a string with options like svg, png, ppt, etc.
            "content": f"{content}",
            "context": f"{context}",
            "language": "en-US",
            #   "style_id": "CDQPRVVJCSTPRBBCD5Q6AWR",
            #   "visual_id": "5UCQJLAV5S6NXEWS2PBJF54UYPW5NZ4G",
            #   "visual_ids": [
                # "5UCQJLAV5S6NXEWS2PBJF54UYPW5NZ4G",
                # "J4Y4APTY6QOLEJEVFXONLK3IQ26S27ZL"
            #   ],
            # "visual_query": "timeline",
            "visual_queries": visual_queries, # this should be a list of strings with options like timeline, flowchart, mindmap, etc.
            "number_of_visuals": number_of_visuals, # this should be equal to the length of the visual_queries list
            "transparent_background": transparent_background,
            "color_mode": color_mode, # this should be a string with options like light, dark, both etc.
            "width": width,
            "height": height,
            "orientation": orientation, # this should be a string with options like auto, horizontal, vertical, square, etc.
            "text_extraction_mode": text_extraction_mode, # this should be a string with options like auto, preserve, rewrite, etc.
            "sort_strategy": sort_strategy # this should be a string with options like relevance, random, etc.
            })
        headers = {
            'Content-Type': 'application/json',
            'Accept': 'application/json',
            'Authorization': f'Bearer {NAPKIN_API_TOKEN}'
        }
        response = requests.request("POST", url, headers=headers, data=payload)
        
        if response.status_code != 200:
            error_body = response.text or "(empty response body)"
            raise Exception(
                f"Napkin AI API returned status {response.status_code}: {error_body}"
            )
        
        if not response.text:
            raise Exception("Napkin AI API returned an empty response body")
        
        return response.json()
    except requests.exceptions.JSONDecodeError as e:
        print(f"Error creating request: Failed to parse Napkin AI response as JSON. "
              f"Status: {response.status_code}, Body: {response.text[:500] if response.text else '(empty)'}")
        raise
    except Exception as e:
        print(f"Error creating request: {e}")
        raise e


def poll_request_status(request_id: str, polling_interval: int = 7, max_retries: int = 5):
    """
    Poll the Napkin AI API to check if the visual generation request is completed.
    
    Args:
        request_id: The ID of the request to poll
        polling_interval: Number of seconds to wait between polls (default: 7)
        max_retries: Maximum number of polling attempts before timing out (default: 60, ~7 minutes)
    
    Returns:
        dict: The completed response with status and visual data including generated_files
        
    Raises:
        Exception: If the request fails or times out
    """
    try:
        url = f"https://api.napkin.ai/v1/visual/{request_id}/status"
        headers = {
            'Accept': 'application/json',
            'Authorization': f'Bearer {NAPKIN_API_TOKEN}'
        }
        
        retries = 0
        while retries < max_retries:
            response = requests.get(url, headers=headers)
            
            if response.status_code == 200:
                data = response.json()
                status = data.get("status")
                
                print(f"Polling attempt {retries + 1}: Status = {status}")
                
                if status == "completed":
                    print(f"Request {request_id} completed successfully!")
                    return data
                elif status == "failed":
                    raise Exception(f"Request {request_id} failed: {data}")
                elif status == "pending":
                    time.sleep(polling_interval)
                    retries += 1
                else:
                    print(f"Unknown status '{status}', continuing to poll...")
                    time.sleep(polling_interval)
                    retries += 1
            else:
                raise Exception(f"Error polling request status: {response.status_code} - {response.text}")
        
        raise Exception(f"Polling timeout: Request {request_id} did not complete after {max_retries * polling_interval} seconds")
        
    except Exception as e:
        print(f"Error polling request status: {e}")
        raise e


def download_generated_file(file_url: str, save_path: str):
    """
    Download a generated visual file from Napkin AI.
    
    Args:
        file_url: The URL of the generated file (from generated_files array)
        save_path: Local path to save the file
        
    Returns:
        str: Path to the saved file
        
    Raises:
        Exception: If download fails
    """
    try:
        headers = {
            'Accept': 'image/svg+xml',
            'Authorization': f'Bearer {NAPKIN_API_TOKEN}'
        }
        
        response = requests.get(file_url, headers=headers)
        
        if response.status_code == 200:
            with open(save_path, 'wb') as f:
                f.write(response.content)
            print(f"File downloaded successfully to: {save_path}")
            return save_path
        else:
            raise Exception(f"Error downloading file: {response.status_code} - {response.text}")
            
    except Exception as e:
        print(f"Error downloading file: {e}")
        raise e


def make_napkin_ai_visual_request(content: str, 
    context: str, 
    format: str = "svg",
    visual_queries: list[str] = None, 
    number_of_visuals: int = 1, 
    transparent_background: bool = False, 
    color_mode: str = "light", 
    width: int = 1200, 
    height: int = 800, 
    orientation: str = "square", 
    text_extraction_mode: str = "preserve", 
    sort_strategy: str = "relevance",
    polling_interval: int = 7,
    max_retries: int = 60,
    save_path: str = None):
    """
    Create a Napkin AI visual request and poll until completion.
    
    This is a convenience function that combines create_request and poll_request_status.
    
    Returns:
        dict: The completed response with visual data
    """
    try:
        initial_response = create_request(
            format=format,
            content=content,
            context=context,
            visual_queries=visual_queries,
            number_of_visuals=number_of_visuals,
            transparent_background=transparent_background,
            color_mode=color_mode,
            width=width,
            height=height,
            orientation=orientation,
            text_extraction_mode=text_extraction_mode,
            sort_strategy=sort_strategy
        )
        
        request_id = initial_response.get("id")
        if not request_id:
            raise Exception("No request ID returned from create_request")
        
        print(f"Request created with ID: {request_id}")
        
        completed_response = poll_request_status(
            request_id=request_id,
            polling_interval=polling_interval,
            max_retries=max_retries
        )
        file_path = download_generated_file(completed_response.get("generated_files")[0].get("url"),save_path)

        return file_path
        
    except Exception as e:
        print(f"Error in create_and_poll_request: {e}")
        raise e
