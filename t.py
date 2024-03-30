import random
import bittensor as bt
from storage.api import store, retrieve
import asyncio

# Store data
wallet = bt.wallet()
subtensor = bt.subtensor()

async def main():
    data = b"Some bytestring data!"
    cid, hotkeys = await store(data, wallet, subtensor, netuid=22)
    print("Stored {} with {} hotkeys".format(cid, hotkeys))

# Run the async function
asyncio.run(main())
