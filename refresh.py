import requests
import json
import progressbar
import sqlalchemy
from sqlalchemy import create_engine, MetaData, Table, Column, String
from sqlalchemy.types import Boolean
from requests.adapters import HTTPAdapter
from requests.packages.urllib3.util.retry import Retry


DATA_TABLENAME = "datasets"


def requests_retry_session(
    retries=10,
    backoff_factor=0.3,
    status_forcelist=(),
    session=None,
):
    session = session or requests.Session()
    retry = Retry(
        total=retries,
        read=retries,
        connect=retries,
        backoff_factor=backoff_factor,
        status_forcelist=status_forcelist,
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount('http://', adapter)
    session.mount('https://', adapter)
    return session


def fetch_datasets():
    results = []
    api_url = "https://iatiregistry.org/api/3/action/package_search?rows=1000"
    response = requests_retry_session().get(url=api_url, timeout=30).content
    json_response = json.loads(response)
    full_count = json_response["result"]["count"]
    current_count = len(json_response["result"]["results"])
    results += [{"id": resource["package_id"], "hash": resource["hash"], "url": resource["url"]} for result in json_response["result"]["results"] for resource in result["resources"]]
    while current_count < full_count:
        next_api_url = "{}&start={}".format(api_url, current_count)
        response = requests_retry_session().get(url=next_api_url, timeout=30).content
        json_response = json.loads(response)
        current_count += len(json_response["result"]["results"])
        results += [{"id": resource["package_id"], "hash": resource["hash"], "url": resource["url"]} for result in json_response["result"]["results"] for resource in result["resources"]]
    return results


def main():
    engine = create_engine('sqlite:///iati.db')
    conn = engine.connect()
    meta = MetaData(engine)
    meta.reflect()

    all_datasets = fetch_datasets()
    new_count = 0

    try:
        datasets = Table(DATA_TABLENAME, meta, autoload=True)
    except sqlalchemy.exc.NoSuchTableError:  # First run
        datasets = Table(
            DATA_TABLENAME,
            meta,
            Column('id', String, primary_key=True),
            Column('hash', String),
            Column('url', String),
            Column('new', Boolean, unique=False, default=True),  # Marks whether a dataset is brand new
            Column('modified', Boolean, unique=False, default=False),  # Marks whether a dataset is old but modified
            Column('stale', Boolean, unique=False, default=False),  # Marks whether a dataset is scheduled for deletion
            Column('error', Boolean, unique=False, default=False)
        )
        meta.create_all(engine)
        new_count += len(all_datasets)
        conn.execute(datasets.insert(), all_datasets)

    all_dataset_ids = [dataset["id"] for dataset in all_datasets]
    cached_datasets = conn.execute(datasets.select()).fetchall()
    cached_dataset_ids = [dataset["id"] for dataset in cached_datasets]
    stale_dataset_ids = list(set(cached_dataset_ids) - set(all_dataset_ids))
    conn.execute(datasets.update().where(datasets.c.id.in_(stale_dataset_ids)).values(new=0, modified=0, stale=1, error=0))

    stale_count = len(stale_dataset_ids)
    modified_count = 0
    bar = progressbar.ProgressBar()
    for dataset in bar(all_datasets):
        try:  # Try to insert new dataset
            dataset["new"] = 1
            dataset["modified"] = 0
            dataset["stale"] = 0
            dataset["error"] = 0
            conn.execute(datasets.insert(dataset))
            new_count += 1
        except sqlalchemy.exc.IntegrityError:  # Dataset ID already exists
            cached_dataset = conn.execute(datasets.select().where(datasets.c.id == dataset["id"])).fetchone()
            if cached_dataset["hash"] == dataset["hash"]:  # If the hashes match, carry on
                continue
            else:  # Otherwise, mark it modified and update the metadata
                dataset["new"] = 0
                dataset["modified"] = 1
                dataset["stale"] = 0  # If for some reason, we pick up a previously stale dataset
                dataset["error"] = 0
                conn.execute(datasets.update().where(datasets.c.id == dataset["id"]).values(dataset))
                modified_count += 1

    engine.dispose()

    print("New: {}; Modified: {}; Stale: {}".format(new_count, modified_count, stale_count))


if __name__ == '__main__':
    main()
