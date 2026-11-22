from dataclasses import dataclass, field


@dataclass
class OpeningHours:
    Monday: str | None = None
    Tuesday: str | None = None
    Wednesday: str | None = None
    Thursday: str | None = None
    Friday: str | None = None
    Saturday: str | None = None
    Sunday: str | None = None

    @classmethod
    def from_api(cls, data: object) -> "OpeningHours | None":
        if not isinstance(data, dict):
            return None
        known = {f.name for f in cls.__dataclass_fields__.values()}
        return cls(**{key: str(value) for key, value in data.items() if key in known})


@dataclass
class Place:
    """One place of a Serper maps answer, with Serper's own field names."""

    position: int = 0
    title: str = ""
    latitude: float = 0.0
    longitude: float = 0.0
    rating: float | None = None
    ratingCount: int | None = None
    type: str = ""
    types: list[str] = field(default_factory=list)
    cid: str = ""
    fid: str = ""
    placeId: str = ""
    address: str | None = None
    website: str | None = None
    phoneNumber: str | None = None
    openingHours: OpeningHours | None = None
    bookingLinks: list[str] | None = None
    thumbnailUrl: str | None = None
    isUnclaimedListing: bool | None = None

    @classmethod
    def from_api(cls, data: object) -> "Place | None":
        """Build a place from Serper's JSON, dropping fields this type does not know."""
        if not isinstance(data, dict):
            return None
        known = {f.name for f in cls.__dataclass_fields__.values()}
        values = {key: value for key, value in data.items() if key in known}
        values["types"] = [str(item) for item in values.get("types") or []]
        values["openingHours"] = OpeningHours.from_api(values.get("openingHours"))
        return cls(**values)


@dataclass
class SearchParameters:
    q: str = ""
    type: str = ""
    num: int = 0
    page: int = 0
    ll: str = ""
    engine: str = ""


@dataclass
class SearchResult:
    """One /maps answer: the echoed search, the places, and what it cost."""

    searchParameters: SearchParameters = field(default_factory=SearchParameters)
    ll: str = ""
    places: list[Place] = field(default_factory=list)
    credits: int = 0

    @classmethod
    def from_api(cls, data: dict) -> "SearchResult":
        parameters = data.get("searchParameters")
        parameters = parameters if isinstance(parameters, dict) else {}
        known = {f.name for f in SearchParameters.__dataclass_fields__.values()}
        return cls(
            searchParameters=SearchParameters(
                **{k: v for k, v in parameters.items() if k in known}
            ),
            ll=str(data.get("ll") or ""),
            places=[
                place
                for item in data.get("places") or []
                if (place := Place.from_api(item)) is not None
            ],
            credits=int(data.get("credits") or 0),
        )
