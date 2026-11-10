"""Reads business details from the JSON-LD blocks of one page."""

from ..document import Document
from ..findings import Finding, json_ld_items, schema_types

# schema.org Organization and LocalBusiness plus all their subtypes.
BUSINESS_TYPES = frozenset(
    {
        "AccountingService",
        "AdultEntertainment",
        "Airline",
        "AmusementPark",
        "AnimalShelter",
        "ArchiveOrganization",
        "ArtGallery",
        "Attorney",
        "AutoBodyShop",
        "AutoDealer",
        "AutoPartsStore",
        "AutoRental",
        "AutoRepair",
        "AutoWash",
        "AutomatedTeller",
        "AutomotiveBusiness",
        "Bakery",
        "BankOrCreditUnion",
        "BarOrPub",
        "BeautySalon",
        "BedAndBreakfast",
        "BikeStore",
        "BookStore",
        "BowlingAlley",
        "Brewery",
        "CafeOrCoffeeShop",
        "Campground",
        "Casino",
        "ChildCare",
        "ClothingStore",
        "CollegeOrUniversity",
        "ComedyClub",
        "ComputerStore",
        "Consortium",
        "ConvenienceStore",
        "Cooperative",
        "Corporation",
        "CovidTestingFacility",
        "DanceGroup",
        "DaySpa",
        "Dentist",
        "DepartmentStore",
        "DiagnosticLab",
        "Distillery",
        "DryCleaningOrLaundry",
        "EducationalOrganization",
        "Electrician",
        "ElectronicsStore",
        "ElementarySchool",
        "EmergencyService",
        "EmploymentAgency",
        "EntertainmentBusiness",
        "ExerciseGym",
        "FastFoodRestaurant",
        "FinancialService",
        "FireStation",
        "Florist",
        "FoodEstablishment",
        "FundingAgency",
        "FundingScheme",
        "FurnitureStore",
        "GardenStore",
        "GasStation",
        "GeneralContractor",
        "GolfCourse",
        "GovernmentOffice",
        "GovernmentOrganization",
        "GymOrFitnessCenter",
        "GroceryStore",
        "HVACBusiness",
        "HairSalon",
        "HardwareStore",
        "HealthAndBeautyBusiness",
        "HealthClub",
        "HighSchool",
        "HobbyShop",
        "HomeAndConstructionBusiness",
        "HomeGoodsStore",
        "Hospital",
        "Hostel",
        "Hotel",
        "HousePainter",
        "IceCreamShop",
        "IndividualPhysician",
        "InsuranceAgency",
        "InternetCafe",
        "JewelryStore",
        "LegalService",
        "Library",
        "LibrarySystem",
        "LiquorStore",
        "LocalBusiness",
        "Locksmith",
        "LodgingBusiness",
        "MedicalBusiness",
        "MedicalClinic",
        "MedicalOrganization",
        "MensClothingStore",
        "MiddleSchool",
        "MobilePhoneStore",
        "Motel",
        "MotorcycleDealer",
        "MotorcycleRepair",
        "MovieRentalStore",
        "MovieTheater",
        "MovingCompany",
        "MusicGroup",
        "MusicStore",
        "NGO",
        "NailSalon",
        "NewsMediaOrganization",
        "NightClub",
        "Notary",
        "OfficeEquipmentStore",
        "OnlineBusiness",
        "OnlineMarketplace",
        "OnlineStore",
        "Optician",
        "Organization",
        "OutletStore",
        "PawnShop",
        "PerformingGroup",
        "PetStore",
        "Pharmacy",
        "Physician",
        "PhysiciansOffice",
        "Plumber",
        "PoliceStation",
        "PoliticalParty",
        "PostOffice",
        "Preschool",
        "ProfessionalService",
        "Project",
        "PublicSwimmingPool",
        "RadioStation",
        "RealEstateAgent",
        "RecyclingCenter",
        "ResearchOrganization",
        "ResearchProject",
        "Resort",
        "Restaurant",
        "RoofingContractor",
        "School",
        "SearchRescueOrganization",
        "SelfStorage",
        "ShoeStore",
        "ShoppingCenter",
        "SkiResort",
        "SportingGoodsStore",
        "SportsActivityLocation",
        "SportsClub",
        "SportsOrganization",
        "SportsTeam",
        "StadiumOrArena",
        "Store",
        "TattooParlor",
        "TelevisionStation",
        "TennisComplex",
        "TheaterGroup",
        "TireShop",
        "TouristInformationCenter",
        "ToyStore",
        "TravelAgency",
        "VacationRental",
        "VeterinaryCare",
        "WholesaleStore",
        "Winery",
        "WorkersUnion",
    }
)


def _text(value) -> str:
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, dict):
        return _text(value.get("url") or value.get("name"))
    if isinstance(value, list):
        return "; ".join(part for part in map(_text, value) if part)
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return str(value)
    return ""


def _opening_hours(node: dict) -> str:
    value = _text(node.get("openingHours"))
    if value:
        return value
    specs = node.get("openingHoursSpecification")
    specs = specs if isinstance(specs, list) else [specs]
    parts = []
    for spec in specs:
        if not isinstance(spec, dict):
            continue
        days = spec.get("dayOfWeek")
        days = (
            ",".join(filter(None, map(_text, days)))
            if isinstance(days, list)
            else _text(days)
        )
        opens, closes = _text(spec.get("opens")), _text(spec.get("closes"))
        if days and opens and closes:
            parts.append(f"{days} {opens}-{closes}")
    return "; ".join(parts)


def find_business(doc: Document, page_url: str) -> list[Finding]:
    rows: list[Finding] = []
    for node in json_ld_items(doc):
        matching = [name for name in schema_types(node) if name in BUSINESS_TYPES]
        if not matching:
            continue
        extra = {"schema_type": matching[0]}
        values = {
            "name": _text(node.get("name")),
            "opening_hours": _opening_hours(node),
            "price_range": _text(node.get("priceRange")),
            "logo_url": _text(node.get("logo")),
            "founded": _text(node.get("foundingDate")),
        }
        for key, value in values.items():
            finding = Finding("business", key, value, extra=extra)
            if value and finding not in rows:
                rows.append(finding)
    return rows
