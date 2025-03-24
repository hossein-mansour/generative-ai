# Copyright 2025 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""
Async Google Cloud Run function that is triggered through eventarc by a
document written in the `screenshots` Firestore collection.

The schema of the `screenshots` collection is:

{
  image: string; // Path to original screenshot in GCS
  ID: string; // UUID for this transaction
  type: string; // MIME type (e.g., image/png)
  timestamp?: Date; // Date/time of image upload
}

Upon receiving an event, the image pointed to by the `image` field is processed
through Gemini and the resulting calendar entry is written to the `state` collection
with the following schema:

{
  processed: boolean; // Whether the image has been processed
  error: boolean; // Whether there's been an error
  active: boolean; // Whether the screenshot is active in the UI
  image?: string; // Path to original screenshot in GCS
  ID?: string; // UUID for this transaction
  message?: string; // Any messages (e.g., an error message)
  event?: CalendarEvent; // The main fields of a calendar event
  timestamp?: Date; // Date/time of last event update
}
"""

import json
import datetime
import os
import functions_framework
from cloudevents.http import CloudEvent
from google.events.cloud import firestore
from google import genai
from google.api_core.exceptions import GoogleAPICallError
from google.genai.types import (
    GenerateContentConfig,
    Part,
)
from google.protobuf.json_format import MessageToDict
import google.cloud.firestore


# Initialize the Gemini model
MODEL_ID = "gemini-2.0-flash-001"
LOCATION = os.environ.get("LOCATION", "")

# Default location if not specified
if LOCATION == "" or LOCATION is None:
    LOCATION = "europe-west1"

client = genai.Client(vertexai=True, location=LOCATION)

# Initialize Firestore client
db = google.cloud.firestore.Client()

# Define the response schema for the analysis
response_schema = {
    "type": "OBJECT",
    "properties": {
        "summary": {"type": "STRING"},
        "location": {"type": "STRING"},
        "description": {"type": "STRING"},
        "start": {"type": "STRING"},
        "end": {"type": "STRING"},
    },
    "required": ["summary", "description", "start", "end"],
}

# Define the prompt for the analysis
PROMPT_TEMPLATE = """ The current date and time is: {current_datetime}.

Analyze the provided screenshot and extract the following information: 

summary: A brief summary of the event.
location: The location of the event.
start time: The start date and time of the event in YYYY-MM-DDTHH:MM:SS format. Assume the event starts in the future.
end time: The end date and time of the event in YYYY-MM-DDTHH:MM:SS format. Calculate this using the duration, if no duration is mentioned, assume the event is an hour long.
Ensure the start and end objects include the correct timeZone based on the information in the screenshot.
duration: The duration of the event in minutes. This could be also written as mins.Use this to calculate the end time if provided.
Ensure the start and end objects include the correct timeZone based on the information in the screenshot.
description: A short description of the event.

The response should have the following schema:

{{
    "type": "OBJECT",
    "properties": {{
        "summary": {{"type": "STRING"}},
        "location": {{"type": "STRING"}},
        "description": {{"type": "STRING"}},
        "start": {{"type": "STRING"}},
        "end": {{"type": "STRING"}}
    }}
}}

"""


@functions_framework.cloud_event
def image_processor(cloud_event: CloudEvent) -> None:
    """Triggers by a change to a Firestore document.

    Args:
        cloud_event: cloud event with information on the firestore event trigger
    """
    firestore_payload = firestore.DocumentEventData()

    print(f"Function triggered by change to: {cloud_event['source']}")

    print("\nNew  value:")
    print(firestore_payload.value)

    document_data = MessageToDict(firestore_payload.value)

    gcs_url = document_data.get("fields", {}).get("image", {}).get("stringValue")
    mime_type = document_data.get("fields", {}).get("type", {}).get("stringValue")
    document_id = document_data.get("fields", {}).get("ID", {}).get("stringValue")

    if not all([gcs_url, mime_type, document_id]):
        print(f"Missing required fields in document: {document_data}")
        return

    # Get the current date and time
    current_datetime = datetime.datetime.now().isoformat()

    # Format the prompt with the current date and time
    prompt = PROMPT_TEMPLATE.format(current_datetime=current_datetime)

    response = client.models.generate_content(
        model=MODEL_ID,
        contents=[
            Part.from_uri(file_uri=gcs_url, mime_type=mime_type),
            prompt,
        ],
        config=GenerateContentConfig(
            response_mime_type="application/json", response_schema=response_schema
        ),
    )

    print(f"Raw Gemini Response: {response.text}")
    event_data = json.loads(response.text)
    print(event_data)

    # firestore document
    firestore_document = {"processed": True, "event": event_data}

    # Write the event data to Firestore
    try:
        doc_ref = db.collection("state").document(document_id)
        doc_ref.set(firestore_document, merge=True)
        print(f"Successfully wrote data to Firestore document: {document_id}")
    except GoogleAPICallError as e:
        print(f"Error writing to Firestore: {e}")
        return
