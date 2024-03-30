import asyncio
from storage import StoreUserAPI
import bittensor as bt

async def main():
    # Load the wallet desired
    wallet = bt.wallet()
    store = StoreUserAPI(wallet)
    # Fetch the subnet 21 validator set via metagraph
    metagraph = bt.metagraph(netuid=21)

    # Store data on the decentralized network!
    cid = await store(
       metagraph=metagraph,
       # add any arguments for the `StoreUser` synapse
       data=b"some data", # Any data (must be bytes) to store
       encrypt=True, # encrpyt the data using the bittensor wallet provided
       ttl=60 * 60 * 24 * 30,
       encoding="utf-8",
       uid=None, # query a specific validator UID if desired
    )

    print(cid)

asyncio.run(main())
