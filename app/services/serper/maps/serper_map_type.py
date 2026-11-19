import json
import os
from dataclasses import dataclass, field

import requests


@dataclass
class SearchParameters:
    q: str
    type: str
    num: int
    page: int
    ll: str
    engine: str


@dataclass
class OpeningHours:
    Wednesday: str | None = None
    Thursday: str | None = None
    Friday: str | None = None
    Saturday: str | None = None
    Sunday: str | None = None
    Monday: str | None = None
    Tuesday: str | None = None


@dataclass
class Place:
    position: int
    title: str
    latitude: float
    longitude: float
    rating: float
    ratingCount: int
    type: str
    types: list[str]
    cid: str
    fid: str
    placeId: str
    address: str | None = None
    website: str | None = None
    phoneNumber: str | None = None
    openingHours: OpeningHours | None = None
    bookingLinks: list[str] | None = None
    thumbnailUrl: str | None = None
    isUnclaimedListing: bool | None = None


@dataclass
class SearchResult:
    searchParameters: SearchParameters
    ll: str
    places: list[Place] = field(default_factory=list)
    credits: int = 0


api_key = os.getenv("SERPER_API")
base_url = "https://google.serper.dev"

url = f"{base_url}/maps"

payload = json.dumps(
    [
        {
            "q": "apple inc",
            "ll": "@-27.481944,153.051111,10z",
            "page": 2,
        },  # 3 credits cost
        {
            "q": "google inc",
            "ll": "@-27.481944,153.051111,10z",
            "page": 2,
        },  # 3 credits cost
        {
            "q": "tesla inc",
            "ll": "@-27.481944,153.051111,10z",
            "page": 2,
        },  # 3 credits cost
    ]
)
headers = {"X-API-KEY": api_key, "Content-Type": "application/json"}

response = requests.request("POST", url, headers=headers, json=payload)

print(response.text)
