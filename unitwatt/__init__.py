"""UnitWatt: a software-only energy and carbon ledger for Indian MSME factories.

The package turns the data a factory already has (electricity bills, meter load surveys,
fuel purchases and production registers) into energy per unit of product, a tariff-aware
schedule, audit-ready proof of savings and product-level embedded emissions.
"""

from dotenv import load_dotenv

load_dotenv()  # reads .env in the project root (if present) into os.environ

__version__ = "0.1.0"
