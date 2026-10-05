# ruff: noqa
# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import base64
import datetime
import json
import os
import urllib.parse
import urllib.request
import uuid
from pathlib import Path
from zoneinfo import ZoneInfo

from a2ui.basic_catalog.provider import BasicCatalog
from a2ui.schema.manager import A2uiSchemaManager
from google import genai
from google.adk.agents import Agent
from google.adk.agents.callback_context import CallbackContext
from google.adk.apps import App
from google.adk.code_executors import AgentEngineSandboxCodeExecutor
from google.adk.memory import VertexAiMemoryBankService
from google.adk.models import Gemini
from google.adk.tools import ToolContext
from google.adk.tools.preload_memory_tool import PreloadMemoryTool
from google.cloud import firestore, storage
from google.genai import types

from app.a2ui_utils import a2ui_callback

# Hardcoded project ID, GCS bucket name, and Memory Bank ID
FIRESTORE_PROJECT = "qwiklabs-gcp-01-d154043e31eb"
GCS_BUCKET_NAME = "wanderlust-travel-media-qwiklabs-gcp-01-d154043e31eb"
MEMORY_BANK_ID = "278538181152866304"


def _get_firestore_client():
    return firestore.Client(project=FIRESTORE_PROJECT)


def _get_code_executor():
    try:
        metadata_path = Path(__file__).parent.parent / "deployment_metadata.json"
        if metadata_path.exists():
            with open(metadata_path) as f:
                data = json.load(f)
            agent_engine_id = data.get("remote_agent_runtime_id")
            if agent_engine_id:
                return AgentEngineSandboxCodeExecutor(agent_engine_resource_name=agent_engine_id)
    except Exception as e:
        print(f"Warning: Could not initialize AgentEngineSandboxCodeExecutor: {e}")
    return None


async def generate_memories_callback(callback_context: CallbackContext):
    """WRITE: After each agent turn, extract and send conversation facts/preferences to Vertex AI Memory Bank."""
    try:
        await callback_context.add_session_to_memory()
    except Exception as e:
        print(f"Memory bank note: {e}")
    return None


def memory_service_builder():
    """Memory service builder for deployed Agent Runtime container."""
    return VertexAiMemoryBankService(
        project=FIRESTORE_PROJECT,
        location="us-east1",
        agent_engine_id=MEMORY_BANK_ID,
    )


def search_destinations(query: str = "", tag: str = None, max_daily_budget: float = None) -> list[dict]:
    """Search or list travel destinations stored in the Firestore backend.

    Args:
        query: Optional search keyword in destination name, country, or description.
        tag: Optional category tag filter (e.g. 'culture', 'beaches', 'nature', 'food', 'museums').
        max_daily_budget: Optional maximum average daily budget in USD.

    Returns:
        List of destination dictionaries matching the criteria.
    """
    db = _get_firestore_client()
    docs = db.collection("destinations").stream()
    results = []
    query_lower = query.lower() if query else ""

    for doc in docs:
        data = doc.to_dict()
        data["id"] = doc.id

        # Filter by budget if specified
        if max_daily_budget is not None and data.get("avg_daily_budget_usd", 0) > max_daily_budget:
            continue

        # Filter by tag if specified
        if tag and tag.lower() not in [t.lower() for t in data.get("tags", [])]:
            continue

        # Filter by search query if specified
        if query_lower:
            text = f"{data.get('name', '')} {data.get('country', '')} {data.get('description', '')}".lower()
            if query_lower not in text:
                continue

        results.append(data)

    return results


def get_destination_details(destination_id: str) -> dict:
    """Get detailed information about a specific travel destination by its ID.

    Args:
        destination_id: The unique ID of the destination (e.g., 'kyoto-japan', 'paris-france', 'banff-canada', 'santorini-greece').

    Returns:
        A dictionary containing destination details or an error message if not found.
    """
    db = _get_firestore_client()
    doc = db.collection("destinations").document(destination_id).get()
    if doc.exists:
        data = doc.to_dict()
        data["id"] = doc.id
        return data
    return {"error": f"Destination with ID '{destination_id}' not found in database."}


def save_destination(
    destination_id: str,
    name: str,
    country: str,
    description: str,
    tags: list[str],
    best_season: str = "Year-round",
    avg_daily_budget_usd: float = 150.0,
    rating: float = 4.5,
) -> dict:
    """Save or update a travel destination in the Firestore database.

    Args:
        destination_id: Unique string identifier (e.g., 'tokyo-japan' or 'rome-italy').
        name: Name of the city/destination.
        country: Country where the destination is located.
        description: Brief summary highlighting key attractions and culture.
        tags: List of descriptive tags (e.g., ['culture', 'food']).
        best_season: Recommended travel season.
        avg_daily_budget_usd: Estimated average daily travel cost in USD.
        rating: User rating score from 1.0 to 5.0.

    Returns:
        Status dict confirming the saved destination record.
    """
    db = _get_firestore_client()
    item = {
        "id": destination_id,
        "name": name,
        "country": country,
        "description": description,
        "tags": tags,
        "best_season": best_season,
        "avg_daily_budget_usd": avg_daily_budget_usd,
        "rating": rating,
    }
    db.collection("destinations").document(destination_id).set(item)
    return {"status": "success", "message": f"Successfully saved destination '{name}' ({destination_id}) to Firestore.", "data": item}


def calculate_trip_budget(destination_id: str, num_days: int, num_travelers: int = 1, style: str = "moderate") -> dict:
    """Calculate an estimated trip budget breakdown for a destination.

    Args:
        destination_id: The unique ID of the destination (e.g., 'kyoto-japan', 'paris-france', 'banff-canada').
        num_days: Duration of the trip in days.
        num_travelers: Number of people traveling.
        style: Travel style tier ('budget', 'moderate', or 'luxury').

    Returns:
        A dictionary containing itemized budget breakdown (lodging, dining, activities, transport) and total estimated cost in USD.
    """
    db = _get_firestore_client()
    doc = db.collection("destinations").document(destination_id).get()

    if not doc.exists:
        return {"error": f"Destination with ID '{destination_id}' not found."}

    data = doc.to_dict()
    base_daily_budget = data.get("avg_daily_budget_usd", 150.0)

    # Style tier multiplier
    style_multipliers = {
        "budget": 0.7,
        "moderate": 1.0,
        "luxury": 1.8,
    }
    multiplier = style_multipliers.get(style.lower(), 1.0)
    adjusted_daily_per_person = base_daily_budget * multiplier

    # Category allocations per person per day
    lodging_daily = adjusted_daily_per_person * 0.40
    food_daily = adjusted_daily_per_person * 0.30
    activities_daily = adjusted_daily_per_person * 0.20
    transport_daily = adjusted_daily_per_person * 0.10

    total_per_person = adjusted_daily_per_person * num_days
    grand_total = total_per_person * num_travelers

    return {
        "destination_id": destination_id,
        "destination_name": data.get("name"),
        "country": data.get("country"),
        "num_days": num_days,
        "num_travelers": num_travelers,
        "travel_style": style,
        "daily_cost_per_person_usd": round(adjusted_daily_per_person, 2),
        "total_estimated_cost_usd": round(grand_total, 2),
        "itemized_breakdown_usd": {
            "lodging": round(lodging_daily * num_days * num_travelers, 2),
            "food_and_dining": round(food_daily * num_days * num_travelers, 2),
            "activities_and_sightseeing": round(activities_daily * num_days * num_travelers, 2),
            "local_transportation": round(transport_daily * num_days * num_travelers, 2),
        },
    }


def convert_currency(amount_usd: float, target_currency: str = "EUR") -> dict:
    """Convert an amount in USD to a foreign travel currency using real-time exchange rates.

    Args:
        amount_usd: Amount in USD to convert (e.g. 100.0 or 500.0).
        target_currency: 3-letter target currency code (e.g., 'EUR', 'JPY', 'GBP', 'CAD', 'AUD').

    Returns:
        A dictionary containing the exchange rate, converted total, and as-of date.
    """
    try:
        currency = target_currency.upper().strip()
        api_key = os.environ.get("CURRENCY_API_KEY")

        # Public Frankfurter API endpoint
        url = f"https://api.frankfurter.app/latest?from=USD&to={currency}"
        headers = {"User-Agent": "WanderlustAgent/1.0"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"

        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=5) as response:
            data = json.loads(response.read().decode())

        rates = data.get("rates", {})
        rate = rates.get(currency)
        if rate is None:
            return {"error": f"Currency '{currency}' is not supported or was not found."}

        converted = round(amount_usd * rate, 2)
        return {
            "amount_usd": amount_usd,
            "target_currency": currency,
            "exchange_rate": rate,
            "converted_amount": converted,
            "as_of_date": data.get("date"),
        }
    except Exception as e:
        return {"error": f"Failed to convert currency to {target_currency}: {str(e)}"}


def get_live_weather(city_name: str) -> dict:
    """Fetch live weather data for any destination city worldwide using Open-Meteo REST API.

    Args:
        city_name: The name of the city/destination (e.g. 'Kyoto', 'Paris', 'Rome', 'San Francisco').

    Returns:
        A dictionary with live temperature (°C and (°F), weather condition, wind speed, and location name.
    """
    try:
        # 1. Geocode city name to lat/lon
        encoded_city = urllib.parse.quote(city_name.strip())
        geo_url = f"https://geocoding-api.open-meteo.com/v1/search?name={encoded_city}&count=1&language=en&format=json"

        req = urllib.request.Request(geo_url, headers={"User-Agent": "WanderlustAgent/1.0"})
        with urllib.request.urlopen(req, timeout=5) as response:
            geo_data = json.loads(response.read().decode())

        results = geo_data.get("results")
        if not results:
            return {"error": f"Could not find coordinates for city '{city_name}'."}

        first_hit = results[0]
        lat = first_hit["latitude"]
        lon = first_hit["longitude"]
        resolved_name = first_hit.get("name", city_name)
        country = first_hit.get("country", "")

        # 2. Fetch current weather forecast
        weather_url = f"https://api.open-meteo.com/v1/forecast?latitude={lat}&longitude={lon}&current_weather=true"
        req_w = urllib.request.Request(weather_url, headers={"User-Agent": "WanderlustAgent/1.0"})
        with urllib.request.urlopen(req_w, timeout=5) as resp_w:
            w_data = json.loads(resp_w.read().decode())

        current = w_data.get("current_weather", {})
        temp_c = current.get("temperature")
        temp_f = round(temp_c * 9 / 5 + 32, 1) if temp_c is not None else None
        wind_speed = current.get("windspeed")

        return {
            "city": resolved_name,
            "country": country,
            "temperature_celsius": temp_c,
            "temperature_fahrenheit": temp_f,
            "windspeed_kmh": wind_speed,
            "is_day": bool(current.get("is_day", 1)),
        }
    except Exception as e:
        return {"error": f"Failed to fetch live weather for '{city_name}': {str(e)}"}


async def generate_destination_image(prompt: str, tool_context: ToolContext) -> dict:
    """Generate a scenic photo for a travel destination using Gemini image model, save it to session artifacts, and upload it to public Cloud Storage.

    Args:
        prompt: Detailed description of the travel photo to generate (e.g., 'A vibrant photo of Santorini cliffside village at sunset').
        tool_context: ADK ToolContext injected automatically by framework.

    Returns:
        A dictionary containing the public GCS URL of the generated image and artifact filename.
    """
    try:
        genai_client = genai.Client(vertexai=True, location="global", project=FIRESTORE_PROJECT)

        response = genai_client.models.generate_content(
            model="gemini-3.1-flash-lite-image",
            contents=prompt,
            config=types.GenerateContentConfig(
                response_modalities=["IMAGE"],
            ),
        )

        candidate_parts = response.candidates[0].content.parts
        image_bytes = None
        mime_type = "image/jpeg"

        for part in candidate_parts:
            if part.inline_data and part.inline_data.data:
                image_bytes = part.inline_data.data
                if part.inline_data.mime_type:
                    mime_type = part.inline_data.mime_type
                break

        if not image_bytes:
            return {"error": "No image data was generated by model."}

        # 1. Save artifact to Playground's Artifacts panel via tool_context
        artifact_filename = f"destination_{uuid.uuid4().hex[:8]}.jpg"
        artifact_part = types.Part.from_bytes(data=image_bytes, mime_type=mime_type)
        await tool_context.save_artifact(filename=artifact_filename, artifact=artifact_part)

        # 2. Upload same image bytes directly to public GCS bucket
        storage_client = storage.Client(project=FIRESTORE_PROJECT)
        bucket = storage_client.bucket(GCS_BUCKET_NAME)
        object_name = f"destinations/{artifact_filename}"
        blob = bucket.blob(object_name)
        blob.upload_from_string(image_bytes, content_type=mime_type)

        public_url = f"https://storage.googleapis.com/{GCS_BUCKET_NAME}/{object_name}"

        return {
            "status": "success",
            "prompt": prompt,
            "artifact_filename": artifact_filename,
            "public_url": public_url,
        }
    except Exception as e:
        return {"error": f"Failed to generate destination image: {str(e)}"}


async def generate_destination_video(prompt: str, tool_context: ToolContext) -> dict:
    """Generate a short travel video clip for a destination using Google's Omni model (gemini-omni-flash-preview) in the global region. Save it to session artifacts and upload to public Cloud Storage.

    Args:
        prompt: Detailed description of the video clip to generate (e.g., 'A short 5-second video of Kyoto cherry blossoms gently swaying in spring').
        tool_context: ADK ToolContext injected automatically by framework.

    Returns:
        A dictionary containing the public GCS URL of the generated video and artifact filename.
    """
    try:
        genai_client = genai.Client(vertexai=True, location="global", project=FIRESTORE_PROJECT)

        interaction = genai_client.interactions.create(
            model="gemini-omni-flash-preview",
            input=prompt,
        )

        video_bytes = None
        mime_type = "video/mp4"

        if hasattr(interaction, "output_video") and interaction.output_video:
            video_obj = interaction.output_video
            if hasattr(video_obj, "data") and video_obj.data:
                data = video_obj.data
                video_bytes = base64.b64decode(data) if isinstance(data, str) else data
            if hasattr(video_obj, "mime_type") and video_obj.mime_type:
                mime_type = video_obj.mime_type

        if not video_bytes and hasattr(interaction, "outputs") and interaction.outputs:
            for output in interaction.outputs:
                if hasattr(output, "data") and output.data:
                    data = output.data
                    video_bytes = base64.b64decode(data) if isinstance(data, str) else data
                    break

        if not video_bytes:
            return {"error": "No video data returned from gemini-omni-flash-preview interaction."}

        # 1. Save artifact to Playground's Artifacts panel via tool_context
        artifact_filename = f"destination_video_{uuid.uuid4().hex[:8]}.mp4"
        artifact_part = types.Part.from_bytes(data=video_bytes, mime_type=mime_type)
        await tool_context.save_artifact(filename=artifact_filename, artifact=artifact_part)

        # 2. Upload same video bytes directly to public Cloud Storage bucket
        storage_client = storage.Client(project=FIRESTORE_PROJECT)
        bucket = storage_client.bucket(GCS_BUCKET_NAME)
        object_name = f"videos/{artifact_filename}"
        blob = bucket.blob(object_name)
        blob.upload_from_string(video_bytes, content_type=mime_type)

        public_url = f"https://storage.googleapis.com/{GCS_BUCKET_NAME}/{object_name}"

        return {
            "status": "success",
            "prompt": prompt,
            "artifact_filename": artifact_filename,
            "public_url": public_url,
        }
    except Exception as e:
        return {"error": f"Failed to generate destination video: {str(e)}"}


def get_current_time(query: str) -> str:
    """Simulates getting the current time for a city.

    Args:
        query: The name of the city to get the current time for.

    Returns:
        A string with the current time information.
    """
    if "sf" in query.lower() or "san francisco" in query.lower():
        tz_identifier = "America/Los_Angeles"
    else:
        return f"Sorry, I don't have timezone information for query: {query}."

    tz = ZoneInfo(tz_identifier)
    now = datetime.datetime.now(tz)
    return f"The current time for query {query} is {now.strftime('%Y-%m-%d %H:%M:%S %Z%z')}"


# Build A2UI 0.8 system prompt schema
schema_manager = A2uiSchemaManager(
    version="0.8",
    catalogs=[BasicCatalog.get_config("0.8")],
)

a2ui_instruction = schema_manager.generate_system_prompt(
    role_description="You are Wanderlust Travel Concierge, a helpful AI travel advisor. You remember user travel preferences, budget styles, dietary needs, and favorite destinations across conversations. Always consider US State Department travel advisories and travel bans when recommending travel destinations, and provide relevant safety guidance or official travel warning context for recommended countries.",
    workflow_description="Analyze the request, search recommendations, calculate travel budgets or weather, generate photos, and return structured A2UI UI when appropriate.",
    ui_description=(
        "Keep every surface tiny and flat: ONE Card > ONE Column > a few Text rows. "
        "Never nest a Card inside a Card. "
        "Use ONLY these components: Card, Column, Row, Text, and Image. Do not use "
        "Table or Heading (unsupported), or Buttons, actions, or forms (they do "
        "nothing in adk web). "
        "You may include one Image component, but only when you have a public https "
        "URL for the image (for example the URL an image tool returns after uploading "
        "to a public bucket). Set the Image url to that exact https link, for example "
        '{"Image": {"url": {"literalString": "https://..."}}}. Never point an '
        "Image at a bare filename, an artifact name, or a non-http(s) path. If you do "
        "not have a public URL, add a short Text line noting the image instead. "
        "No markdown in text; use the usageHint property ('h1', 'h2', 'body') for "
        "headings and emphasis. "
        "Output ONLY the raw A2UI JSON array — no prose, and never wrap it in "
        "<a2a_datapart_json> tags or 'kind'/'data'/'metadata' objects."
    ),
    include_schema=True,
    include_examples=True,
)


root_agent = Agent(
    name="root_agent",
    model=Gemini(
        model="gemini-2.5-flash",
        retry_options=types.HttpRetryOptions(attempts=3),
    ),
    instruction=a2ui_instruction,
    code_executor=_get_code_executor(),
    tools=[
        PreloadMemoryTool(),
        search_destinations,
        get_destination_details,
        save_destination,
        calculate_trip_budget,
        convert_currency,
        get_live_weather,
        generate_destination_image,
        generate_destination_video,
        get_current_time,
    ],
    after_agent_callback=generate_memories_callback,
    after_model_callback=a2ui_callback,
)

app = App(
    root_agent=root_agent,
    name="app",
)
