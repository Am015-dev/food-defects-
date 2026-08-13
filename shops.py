"""Supermarkets on e-food.gr tracked by the dashboard, all within ~5km of
Chalandri.

Discovered via e-food's public nearby-restaurants search centered on
Chalandri (38.0207, 23.7999), filtered to businesses tagged with the
"Supermarket" cuisine, then filtered again by real straight-line distance
(the search API's own "distance" field turned out to be unreliable --
another one of e-food's data bugs -- so distances below were computed
from each store's own lat/long instead). A few further-out results
(Carrefour in Pagkrati, Sklavenitis in Spata, ~7-11km away) were dropped
as not actually "close to Chalandri".

"slug" is the shop's public page path on e-food.gr, taken from the same
search response; the full URL is https://www.e-food.gr/delivery{slug}.
It's what makes a dashboard row clickable through to the real listing.
"""

SHOPS = [
    {
        "id": 8142028,
        "label": "Μασούτης - Αριστοτέλους",  # 0.41km
        "slug": "/xalandri/masoytis-chalandri-aristotelous-73-8142028",
    },
    {
        "id": 8737489,
        "label": "BAZAAR - Λ. Πεντέλης",  # 0.49km
        "slug": "/xalandri/bazaar-8737489",
    },
    {
        "id": 7871734,
        "label": "My Market - Βασ. Γεωργίου",  # 0.52km
        "slug": "/xalandri/my-market-7871734",
    },
    {
        "id": 9038526,
        "label": "Μασούτης - Σοφοκλή Βενιζέλου",  # 0.80km
        "slug": "/xalandri/masoytis-9038526",
    },
    {
        "id": 8681812,
        "label": "My Market Local - Λ. Πεντέλης",  # 1.24km
        "slug": "/xalandri/my-market-local-8681812",
    },
    {
        "id": 7126128,
        "label": "ΑΒ Βασιλόπουλος - Λ. Πεντέλης",  # 1.35km
        "slug": "/xalandri/ab-vassilopoulos-7126128",
    },
    {
        "id": 9181383,
        "label": "Market In - Μεταμορφώσεως",  # 2.11km
        "slug": "/xalandri/market-in-9181383",
    },
    {
        "id": 8414922,
        "label": "ΚΡΗΤΙΚΟΣ - Αγία Παρασκευή",  # 2.84km
        "slug": "/agia-paraskeui/kritikos-8414922",
    },
    {
        "id": 9115944,
        "label": "My Market Local - Νέα Ιωνία",  # 3.16km
        "slug": "/nea-iwnia/my-market-local-9115944",
    },
    {
        "id": 8785275,
        "label": "Γαλαξίας - Βριλήσσια",  # 3.84km
        "slug": "/brilissia/galaxias-8785275",
    },
    {
        "id": 9038607,
        "label": "Μασούτης - Μαρούσι",  # 3.98km
        "slug": "/marousi/masoytis-9038607",
    },
    {
        "id": 6245632,
        "label": "Σκλαβενίτης Express - Γέρακας",  # 4.22km
        "slug": "/gerakas/sklavenitis-express-6245632",
    },
]

SHOP_URLS = {s["id"]: f"https://www.e-food.gr/delivery{s['slug']}" for s in SHOPS}
