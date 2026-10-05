import subprocess
from google.oauth2.credentials import Credentials
from google.cloud import firestore

FIRESTORE_PROJECT = "qwiklabs-gcp-01-d154043e31eb"

def get_db():
    try:
        # Try default client first
        return firestore.Client(project=FIRESTORE_PROJECT)
    except Exception:
        # Fallback to gcloud access token
        token = subprocess.check_output(["gcloud", "auth", "print-access-token"]).decode().strip()
        creds = Credentials(token)
        return firestore.Client(project=FIRESTORE_PROJECT, credentials=creds)

def seed():
    db = get_db()
    destinations = [
        {
            "id": "kyoto-japan",
            "name": "Kyoto",
            "country": "Japan",
            "description": "Historic city known for classical Buddhist temples, gardens, imperial palaces, Shinto shrines, and traditional wooden houses.",
            "tags": ["culture", "temples", "food", "history"],
            "best_season": "Spring",
            "avg_daily_budget_usd": 160.0,
            "rating": 4.9,
        },
        {
            "id": "paris-france",
            "name": "Paris",
            "country": "France",
            "description": "Global center for art, fashion, gastronomy, and culture with iconic landmarks like the Eiffel Tower and Louvre Museum.",
            "tags": ["art", "museums", "romance", "food"],
            "best_season": "Spring / Autumn",
            "avg_daily_budget_usd": 200.0,
            "rating": 4.8,
        },
        {
            "id": "banff-canada",
            "name": "Banff",
            "country": "Canada",
            "description": "Resort town in the province of Alberta, located within Banff National Park in the Rocky Mountains with turquoise glacial lakes.",
            "tags": ["nature", "hiking", "mountains", "adventure"],
            "best_season": "Summer / Winter",
            "avg_daily_budget_usd": 180.0,
            "rating": 4.9,
        },
        {
            "id": "santorini-greece",
            "name": "Santorini",
            "country": "Greece",
            "description": "Cycladic island in the Aegean Sea famous for its white-washed cliffside villages, blue-domed churches, and stunning sunsets.",
            "tags": ["beaches", "romance", "relaxation", "views"],
            "best_season": "Summer",
            "avg_daily_budget_usd": 220.0,
            "rating": 4.7,
        },
    ]

    collection_ref = db.collection("destinations")
    for item in destinations:
        doc_id = item["id"]
        collection_ref.document(doc_id).set(item)
        print(f"Seeded destination: {doc_id}")

if __name__ == "__main__":
    seed()
