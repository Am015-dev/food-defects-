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
"""

SHOPS = [
    {"id": 8142028, "label": "Μασούτης - Αριστοτέλους"},          # 0.41km
    {"id": 8737489, "label": "BAZAAR - Λ. Πεντέλης"},              # 0.49km
    {"id": 7871734, "label": "My Market - Βασ. Γεωργίου"},         # 0.52km
    {"id": 9038526, "label": "Μασούτης - Σοφοκλή Βενιζέλου"},      # 0.80km
    {"id": 8681812, "label": "My Market Local - Λ. Πεντέλης"},     # 1.24km
    {"id": 7126128, "label": "ΑΒ Βασιλόπουλος - Λ. Πεντέλης"},     # 1.35km
    {"id": 9181383, "label": "Market In - Μεταμορφώσεως"},         # 2.11km
    {"id": 8414922, "label": "ΚΡΗΤΙΚΟΣ - Αγία Παρασκευή"},         # 2.84km
    {"id": 9115944, "label": "My Market Local - Νέα Ιωνία"},       # 3.16km
    {"id": 8785275, "label": "Γαλαξίας - Βριλήσσια"},              # 3.84km
    {"id": 9038607, "label": "Μασούτης - Μαρούσι"},                # 3.98km
    {"id": 6245632, "label": "Σκλαβενίτης Express - Γέρακας"},     # 4.22km
]
