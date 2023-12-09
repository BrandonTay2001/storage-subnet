# The MIT License (MIT)
# Copyright © 2023 Yuma Rao
# Copyright © 2023 philanthrope

# Permission is hereby granted, free of charge, to any person obtaining a copy of this software and associated
# documentation files (the “Software”), to deal in the Software without restriction, including without limitation
# the rights to use, copy, modify, merge, publish, distribute, sublicense, and/or sell copies of the Software,
# and to permit persons to whom the Software is furnished to do so, subject to the following conditions:

# The above copyright notice and this permission notice shall be included in all copies or substantial portions of
# the Software.

# THE SOFTWARE IS PROVIDED “AS IS”, WITHOUT WARRANTY OF ANY KIND, EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO
# THE WARRANTIES OF MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL
# THE AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION
# OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER
# DEALINGS IN THE SOFTWARE.

import os
import sys
import copy
import json
import time
import torch
import base64
import typing
import asyncio
import aioredis
import argparse
import traceback
import bittensor as bt

from loguru import logger
from pprint import pformat
from functools import partial
from pyinstrument import Profiler
from traceback import print_exception
from random import choice as random_choice
from Crypto.Random import get_random_bytes, random

from dataclasses import asdict
from storage.validator.event import EventSchema

from storage import protocol

from storage.shared.ecc import (
    hash_data,
    setup_CRS,
    ECCommitment,
    ecc_point_to_hex,
    hex_to_ecc_point,
)

from storage.shared.merkle import (
    MerkleTree,
)

from storage.shared.utils import (
    b64_encode,
    b64_decode,
    chunk_data,
    safe_key_search,
)

from storage.validator.utils import (
    make_random_file,
    get_random_chunksize,
    check_uid_availability,
    get_random_uids,
    get_query_miners,
    get_query_validators,
    get_available_query_miners,
    get_current_validtor_uid_round_robin,
)

from storage.validator.encryption import (
    decrypt_data,
    encrypt_data,
)

from storage.validator.verify import (
    verify_store_with_seed,
    verify_challenge_with_seed,
    verify_retrieve_with_seed,
)

from storage.validator.config import config, check_config, add_args

from storage.validator.state import (
    should_checkpoint,
    checkpoint,
    should_reinit_wandb,
    reinit_wandb,
    load_state,
    save_state,
    init_wandb,
    ttl_get_block,
    log_event,
)

from storage.validator.reward import apply_reward_scores

from storage.validator.weights import (
    should_set_weights,
    set_weights,
)

from storage.validator.database import (
    add_metadata_to_hotkey,
    get_miner_statistics,
    get_metadata_for_hotkey,
    total_network_storage,
    store_chunk_metadata,
    store_file_chunk_mapping_ordered,
    get_metadata_for_hotkey_and_hash,
    update_metadata_for_data_hash,
    get_all_chunk_hashes,
    get_ordered_metadata,
    hotkey_at_capacity,
    get_miner_statistics,
)

from storage.validator.bonding import (
    miner_is_registered,
    update_statistics,
    get_tier_factor,
    compute_all_tiers,
)


class neuron:
    """
    A Neuron instance represents a node in the Bittensor network that performs validation tasks.
    It manages the data validation cycle, including storing, challenging, and retrieving data,
    while also participating in the network consensus.

    Attributes:
        subtensor (bt.subtensor): The interface to the Bittensor network's blockchain.
        wallet (bt.wallet): Cryptographic wallet containing keys for transactions and encryption.
        metagraph (bt.metagraph): Graph structure storing the state of the network.
        database (redis.StrictRedis): Database instance for storing metadata and proofs.
        moving_averaged_scores (torch.Tensor): Tensor tracking performance scores of other nodes.
    """

    @classmethod
    def check_config(cls, config: "bt.Config"):
        check_config(cls, config)

    @classmethod
    def add_args(cls, parser):
        add_args(cls, parser)

    @classmethod
    def config(cls):
        return config(cls)

    subtensor: "bt.subtensor"
    wallet: "bt.wallet"
    metagraph: "bt.metagraph"

    def __init__(self):
        self.config = neuron.config()
        self.check_config(self.config)
        bt.logging(config=self.config, logging_dir=self.config.neuron.full_path)
        print(self.config)
        bt.logging.info("neuron.__init__()")

        # Init device.
        bt.logging.debug("loading device")
        self.device = torch.device(self.config.neuron.device)
        bt.logging.debug(str(self.device))

        # Init subtensor
        bt.logging.debug("loading subtensor")
        self.subtensor = (
            bt.MockSubtensor()
            if self.config.neuron.mock_subtensor
            else bt.subtensor(config=self.config)
        )
        bt.logging.debug(str(self.subtensor))

        # Init wallet.
        bt.logging.debug("loading wallet")
        self.wallet = bt.wallet(config=self.config)
        self.wallet.coldkey  # Unlock for testing
        self.wallet.create_if_non_existent()
        if not self.config.wallet._mock:
            if not self.subtensor.is_hotkey_registered_on_subnet(
                hotkey_ss58=self.wallet.hotkey.ss58_address, netuid=self.config.netuid
            ):
                raise Exception(
                    f"Wallet not currently registered on netuid {self.config.netuid}, please first register wallet before running"
                )

        bt.logging.debug(f"wallet: {str(self.wallet)}")

        # Init metagraph.
        bt.logging.debug("loading metagraph")
        self.metagraph = bt.metagraph(
            netuid=self.config.netuid, network=self.subtensor.network, sync=False
        )  # Make sure not to sync without passing subtensor
        self.metagraph.sync(subtensor=self.subtensor)  # Sync metagraph with subtensor.
        bt.logging.debug(str(self.metagraph))

        # Setup database
        self.database = aioredis.StrictRedis(
            host=self.config.database.host,
            port=self.config.database.port,
            db=self.config.database.index,
        )
        self.db_semaphore = asyncio.Semaphore()

        # Init Weights.
        bt.logging.debug("loading moving_averaged_scores")
        self.moving_averaged_scores = torch.zeros((self.metagraph.n)).to(self.device)
        bt.logging.debug(str(self.moving_averaged_scores))

        self.my_subnet_uid = self.metagraph.hotkeys.index(
            self.wallet.hotkey.ss58_address
        )
        bt.logging.info(f"Running validator on uid: {self.my_subnet_uid}")

        # Dendrite pool for querying the network.
        bt.logging.debug("loading dendrite_pool")
        if self.config.neuron.mock_dendrite_pool:
            self.dendrite = MockDendrite()
        else:
            self.dendrite = bt.dendrite(wallet=self.wallet)
        bt.logging.debug(str(self.dendrite))

        # Init the event loop.
        self.loop = asyncio.get_event_loop()

        # Init wandb.
        if not self.config.wandb.off:
            bt.logging.debug("loading wandb")
            init_wandb(self)

        self.config.neuron.epoch_length = 100
        bt.logging.debug(f"Set epoch_length {self.config.neuron.epoch_length}")

        if self.config.neuron.challenge_sample_size == 0:
            self.config.neuron.challenge_sample_size = self.metagraph.n

        self.prev_step_block = ttl_get_block(self)
        self.step = 0

    async def store_encrypted_data(
        self,
        encrypted_data: typing.Union[bytes, str],
        encryption_payload: dict,
        ttl: int = 0,
    ) -> bool:
        event = EventSchema(
            task_name="Store",
            successful=[],
            completion_times=[],
            task_status_messages=[],
            task_status_codes=[],
            block=self.subtensor.get_current_block(),
            uids=[],
            step_length=0.0,
            best_uid="",
            best_hotkey="",
            rewards=[],
            moving_averaged_scores=[],
        )

        start_time = time.time()

        encrypted_data = (
            encrypted_data.encode("utf-8")
            if isinstance(encrypted_data, str)
            else encrypted_data
        )

        # Setup CRS for this round of validation
        g, h = setup_CRS(curve=self.config.neuron.curve)

        # Hash the data
        data_hash = hash_data(encrypted_data)

        # Convert to base64 for compactness
        # TODO: Don't do this if it's already b64 encoded. (Check first)
        b64_encrypted_data = base64.b64encode(encrypted_data).decode("utf-8")

        if self.config.neuron.verbose:
            bt.logging.debug(f"storing user data: {encrypted_data[:200]}...")
            bt.logging.debug(f"storing user hash: {data_hash}")
            bt.logging.debug(f"b64 encrypted data: {b64_encrypted_data[:200]}...")

        synapse = protocol.Store(
            encrypted_data=b64_encrypted_data,
            curve=self.config.neuron.curve,
            g=ecc_point_to_hex(g),
            h=ecc_point_to_hex(h),
            seed=get_random_bytes(32).hex(),  # 256-bit seed
        )

        # Select subset of miners to query (e.g. redunancy factor of N)
        uids = await get_available_query_miners(
            self, k=self.config.neuron.store_redundancy
        )
        bt.logging.debug(f"store uids: {uids}")
        # Check each UID/axon to ensure it's not at it's storage capacity (e.g. 1TB)
        # before sending another storage request (do not allow higher than tier allow)
        # TODO: keep selecting UIDs until we get N that are not at capacity
        avaialble_uids = [
            uid
            for uid in uids
            if not await hotkey_at_capacity(self.metagraph.hotkeys[uid], self.database)
        ]

        axons = [self.metagraph.axons[uid] for uid in avaialble_uids]
        failed_uids = [None]

        retries = 0
        while len(failed_uids) and retries < 3:
            if failed_uids == [None]:
                # initial loop
                failed_uids = []

            # Broadcast the query to selected miners on the network.
            responses = await self.dendrite(
                axons,
                synapse,
                deserialize=False,
                timeout=self.config.neuron.store_timeout,
            )

            # Log the results for monitoring purposes.
            if self.config.neuron.verbose and self.config.neuron.log_responses:
                bt.logging.debug(f"Initial store round 1.")
                [
                    bt.logging.debug(f"Store response: {response.dendrite.dict()}")
                    for response in responses
                ]

            # Compute the rewards for the responses given proc time.
            rewards: torch.FloatTensor = torch.zeros(
                len(responses), dtype=torch.float32
            ).to(self.device)

            # TODO: Add proper error logging, e.g. if we timeout, raise/show timeout error instead of
            # uniform "failed to verify" error (it doesn't give any context for what may have happened)
            for idx, (uid, response) in enumerate(zip(uids, responses)):
                # Verify the commitment
                hotkey = self.metagraph.hotkeys[uid]
                success = verify_store_with_seed(response)
                if success:
                    bt.logging.debug(
                        f"Successfully verified store commitment from UID: {uid}"
                    )

                    # Prepare storage for the data for particular miner
                    response_storage = {
                        "prev_seed": synapse.seed,
                        "size": sys.getsizeof(
                            encrypted_data
                        ),  # in bytes, not len(data)
                        "encryption_payload": encryption_payload,
                    }
                    bt.logging.trace(
                        f"Storing UID {uid} data {pformat(response_storage)}"
                    )

                    # Store in the database according to the data hash and the miner hotkey
                    await add_metadata_to_hotkey(
                        hotkey,
                        data_hash,
                        response_storage,
                        self.database,
                    )
                    if ttl > 0:
                        await self.database.expire(
                            f"{hotkey}:{data_hash}",
                            ttl,
                        )
                    bt.logging.debug(
                        f"Stored data in database with key: {hotkey} | {data_hash}"
                    )

                else:
                    bt.logging.error(
                        f"Failed to verify store commitment from UID: {uid}"
                    )
                    failed_uids.append(uid)

                # Update the storage statistics
                await update_statistics(
                    ss58_address=hotkey,
                    success=success,
                    task_type="store",
                    database=self.database,
                )

                # Apply reward for this store
                tier_factor = await get_tier_factor(hotkey, self.database)
                rewards[idx] = 1.0 * tier_factor if success else 0.0

                event.successful.append(success)
                event.uids.append(uid)
                event.completion_times.append(response.dendrite.process_time)
                event.task_status_messages.append(response.dendrite.status_message)
                event.task_status_codes.append(response.dendrite.status_code)

            event.rewards.extend(rewards.tolist())

            if self.config.neuron.verbose and self.config.neuron.log_responses:
                bt.logging.debug(f"Store responses round: {retries}")
                [
                    bt.logging.debug(f"Store response: {response.dendrite.dict()}")
                    for response in responses
                ]

            bt.logging.trace(f"Applying store rewards for retry: {retries}")
            apply_reward_scores(
                self,
                uids,
                responses,
                rewards,
                timeout=self.config.neuron.store_timeout,
                mode="minmax",
            )

            # Get a new set of UIDs to query for those left behind
            if failed_uids != []:
                bt.logging.trace(f"Failed to store on uids: {failed_uids}")
                uids = await get_available_query_miners(self, k=len(failed_uids))
                bt.logging.trace(f"Retrying with new uids: {uids}")
                axons = [self.metagraph.axons[uid] for uid in uids]
                failed_uids = []  # reset failed uids for next round
                retries += 1

        # Calculate step length
        end_time = time.time()
        event.step_length = end_time - start_time

        # Determine the best UID based on rewards
        if event.rewards:
            best_index = max(range(len(event.rewards)), key=event.rewards.__getitem__)
            event.best_uid = event.uids[best_index]
            event.best_hotkey = self.metagraph.hotkeys[event.best_uid]

        # Update event log with moving averaged scores
        event.moving_averaged_scores = self.moving_averaged_scores.tolist()

        return event

    async def store_random_data(self):
        """
        Stores data on the network and ensures it is correctly committed by the miners.

        Parameters:
        - data (bytes, optional): The data to be stored.
        - wallet (bt.wallet, optional): The wallet to be used for encrypting the data.

        Returns:
        - The status of the data storage operation.
        """

        # Setup CRS for this round of validation
        g, h = setup_CRS(curve=self.config.neuron.curve)

        # Make a random bytes file to test the miner if none provided
        data = make_random_file(maxsize=self.config.neuron.maxsize)
        bt.logging.debug(f"Random store data size: {sys.getsizeof(data)}")

        # Encrypt the data
        # TODO: create and use a throwaway wallet (never decrypable)
        encrypted_data, encryption_payload = encrypt_data(data, self.wallet)

        return await self.store_encrypted_data(
            encrypted_data, encryption_payload, ttl=self.config.neuron.data_ttl
        )

    async def handle_challenge(
        self, uid: int
    ) -> typing.Tuple[bool, protocol.Challenge]:
        """
        Handles a challenge sent to a miner and verifies the response.

        Parameters:
        - uid (int): The UID of the miner being challenged.

        Returns:
        - Tuple[bool, protocol.Challenge]: A tuple containing the verification result and the challenge.
        """
        hotkey = self.metagraph.hotkeys[uid]
        keys = await self.database.hkeys(f"hotkey:{hotkey}")
        bt.logging.trace(f"{len(keys)} hashes pulled for hotkey {hotkey}")
        if keys == []:
            # Create a dummy response to send back
            dummy_response = protocol.Challenge(
                challenge_hash="",
                chunk_size=0,
                g="",
                h="",
                curve="",
                challenge_index=0,
                seed="",
            )
            return None, [
                dummy_response
            ]  # no data found associated with this miner hotkey

        data_hash = random.choice(keys).decode("utf-8")
        data = await get_metadata_for_hotkey_and_hash(hotkey, data_hash, self.database)

        if self.config.neuron.verbose:
            bt.logging.trace(f"Challenge lookup key: {data_hash}")
            bt.logging.trace(f"Challenge data: {data}")

        try:
            chunk_size = (
                self.config.neuron.override_chunk_size
                if self.config.neuron.override_chunk_size > 0
                else get_random_chunksize(
                    minsize=self.config.neuron.min_chunk_size,
                    maxsize=max(
                        self.config.neuron.min_chunk_size,
                        data["size"] // self.config.neuron.chunk_factor,
                    ),
                )
            )
        except:
            bt.logging.error(
                f"Failed to get chunk size {self.config.neuron.min_chunk_size} | {self.config.neuron.chunk_factor} | {data['size'] // self.config.neuron.chunk_factor}"
            )
            chunk_size = 0

        num_chunks = (
            data["size"] // chunk_size if data["size"] > chunk_size else data["size"]
        )
        if self.config.neuron.verbose:
            bt.logging.trace(f"challenge data size : {data['size']}")
            bt.logging.trace(f"challenge chunk size: {chunk_size}")
            bt.logging.trace(f"challenge num chunks: {num_chunks}")

        # Setup new Common-Reference-String for this challenge
        g, h = setup_CRS()

        synapse = protocol.Challenge(
            challenge_hash=data_hash,
            chunk_size=chunk_size,
            g=ecc_point_to_hex(g),
            h=ecc_point_to_hex(h),
            curve="P-256",
            challenge_index=random.choice(range(num_chunks)),
            seed=get_random_bytes(32).hex(),
        )

        axon = self.metagraph.axons[uid]

        response = await self.dendrite(
            [axon],
            synapse,
            deserialize=True,
            timeout=self.config.neuron.challenge_timeout,
        )
        verified = verify_challenge_with_seed(response[0])

        if verified:
            data["prev_seed"] = synapse.seed
            await update_metadata_for_data_hash(hotkey, data_hash, data, self.database)

        # Record the time taken for the challenge
        return verified, response

    async def challenge(self):
        """
        Initiates a series of challenges to miners, verifying their data storage through the network's consensus mechanism.

        Asynchronously challenge and see who returns the data fastest (passes verification), and rank them highest
        """

        event = EventSchema(
            task_name="Challenge",
            successful=[],
            completion_times=[],
            task_status_messages=[],
            task_status_codes=[],
            block=self.subtensor.get_current_block(),
            uids=[],
            step_length=0.0,
            best_uid=-1,
            best_hotkey="",
            rewards=[],
        )

        start_time = time.time()
        tasks = []
        uids = await get_available_query_miners(
            self, k=self.config.neuron.challenge_sample_size
        )
        bt.logging.debug(f"challenge uids {uids}")
        responses = []
        for uid in uids:
            tasks.append(asyncio.create_task(self.handle_challenge(uid)))
        responses = await asyncio.gather(*tasks)

        if self.config.neuron.verbose and self.config.neuron.log_responses:
            [
                bt.logging.trace(
                    f"Challenge response {uid} | {pformat(response[0].axon.dict())}"
                )
                for uid, response in zip(uids, responses)
            ]

        # Compute the rewards for the responses given the prompt.
        rewards: torch.FloatTensor = torch.zeros(
            len(responses), dtype=torch.float32
        ).to(self.device)

        for idx, (uid, (verified, response)) in enumerate(zip(uids, responses)):
            if self.config.neuron.verbose:
                bt.logging.trace(
                    f"Challenge idx {idx} uid {uid} verified {verified} response {pformat(response[0].axon.dict())}"
                )

            hotkey = self.metagraph.hotkeys[uid]

            if verified == None:
                continue  # We don't have any data for this hotkey, skip it.

            # Update the challenge statistics
            await update_statistics(
                ss58_address=hotkey,
                success=verified,
                task_type="challenge",
                database=self.database,
            )

            # Apply reward for this challenge
            tier_factor = await get_tier_factor(hotkey, self.database)
            rewards[idx] = 1.0 * tier_factor if verified else -0.1 * tier_factor

            # Log the event data for this specific challenge
            event.uids.append(uid)
            event.successful.append(verified)
            event.completion_times.append(response[0].dendrite.process_time)
            event.task_status_messages.append(response[0].dendrite.status_message)
            event.task_status_codes.append(response[0].dendrite.status_code)
            event.rewards.append(rewards[idx].item())

        # Calculate the total step length for all challenges
        event.step_length = time.time() - start_time

        responses = [response[0] for (verified, response) in responses]
        bt.logging.trace("Applying challenge rewards")
        apply_reward_scores(
            self,
            uids,
            responses,
            rewards,
            timeout=self.config.neuron.challenge_timeout,
            mode="minmax",
        )

        # Determine the best UID based on rewards
        if event.rewards:
            best_index = max(range(len(event.rewards)), key=event.rewards.__getitem__)
            event.best_uid = event.uids[best_index]
            event.best_hotkey = self.metagraph.hotkeys[event.best_uid]

        return event

    async def handle_retrieve(self, uid):
        bt.logging.debug(f"handle_retrieve uid: {uid}")
        hotkey = self.metagraph.hotkeys[uid]
        keys = await self.database.hkeys(f"hotkey:{hotkey}")

        if keys == []:
            bt.logging.warning(f"No data found for uid: {uid} | hotkey: {hotkey}")
            # Create a dummy response to send back
            return None, ""

        data_hash = random.choice(keys).decode("utf-8")
        bt.logging.debug(f"handle_retrieve data_hash: {data_hash}")

        data = await get_metadata_for_hotkey_and_hash(hotkey, data_hash, self.database)
        axon = self.metagraph.axons[uid]

        synapse = protocol.Retrieve(
            data_hash=data_hash,
            seed=get_random_bytes(32).hex(),
        )
        response = await self.dendrite(
            [axon],
            synapse,
            deserialize=False,
            timeout=self.config.neuron.retrieve_timeout,
        )

        try:
            bt.logging.trace(f"Fetching AES payload from UID: {uid}")

            # Load the data for this miner from validator storage
            data = await get_metadata_for_hotkey_and_hash(
                hotkey, data_hash, self.database
            )

            # If we reach here, this miner has passed verification. Update the validator storage.
            data["prev_seed"] = synapse.seed
            await update_metadata_for_data_hash(hotkey, data_hash, data, self.database)
            bt.logging.trace(
                f"Updated metadata for UID: {uid} with data: {pformat(data)}"
            )
            # TODO: get a temp link from the server to send back to the client instead

        except Exception as e:
            bt.logging.error(f"Failed to retrieve data from UID: {uid} with error: {e}")

        return response[0], data_hash

    async def retrieve(
        self, data_hash: str = None, yield_event: bool = True
    ) -> typing.Tuple[bytes, typing.Callable]:
        """
        Retrieves and verifies data from the network, ensuring integrity and correctness of the data associated with the given hash.

        Parameters:
            data_hash (str): The hash of the data to be retrieved.

        Returns:
            The retrieved data if the verification is successful.
        """

        # Initialize event schema
        event = EventSchema(
            task_name="Retrieve",
            successful=[],
            completion_times=[],
            task_status_messages=[],
            task_status_codes=[],
            block=self.subtensor.get_current_block(),
            uids=[],
            step_length=0.0,
            best_uid=-1,
            best_hotkey="",
            rewards=[],
            set_weights=[],
        )

        start_time = time.time()

        uids = await get_available_query_miners(
            self, k=self.config.neuron.challenge_sample_size
        )

        # Ensure that each UID has data to retreive. If not, skip it.
        uids = [
            uid
            for uid in uids
            if await get_metadata_for_hotkey(self.metagraph.hotkeys[uid], self.database)
            != {}
        ]
        bt.logging.debug(f"UIDs to query   : {uids}")
        bt.logging.debug(
            f"Hotkeys to query: {[self.metagraph.hotkeys[uid][:5] for uid in uids]}"
        )

        tasks = []
        for uid in uids:
            tasks.append(asyncio.create_task(self.handle_retrieve(uid)))
        response_tuples = await asyncio.gather(*tasks)

        if self.config.neuron.verbose and self.config.neuron.log_responses:
            [
                bt.logging.trace(
                    f"Retrieve response: {uid} | {pformat(response.dendrite.dict())}"
                )
                for uid, (response, _) in zip(uids, response_tuples)
            ]
        rewards: torch.FloatTensor = torch.zeros(
            len(response_tuples), dtype=torch.float32
        ).to(self.device)

        for idx, (uid, (response, data_hash)) in enumerate(zip(uids, response_tuples)):
            hotkey = self.metagraph.hotkeys[uid]

            if response == None:
                continue  # We don't have any data for this hotkey, skip it.

            try:
                decoded_data = base64.b64decode(response.data)
            except Exception as e:
                bt.logging.error(
                    f"Failed to decode data from UID: {uids[idx]} with error {e}"
                )
                rewards[idx] = -0.1

                # Update the retrieve statistics
                await update_statistics(
                    ss58_address=hotkey,
                    success=False,
                    task_type="retrieve",
                    database=self.database,
                )
                continue

            if str(hash_data(decoded_data)) != data_hash:
                bt.logging.error(
                    f"Hash of recieved data does not match expected hash! {str(hash_data(decoded_data))} != {data_hash}"
                )
                rewards[idx] = -0.1

                # Update the retrieve statistics
                await update_statistics(
                    ss58_address=hotkey,
                    success=False,
                    task_type="retrieve",
                    database=self.database,
                )
                continue

            success = verify_retrieve_with_seed(response)
            if not success:
                bt.logging.error(
                    f"data verification failed! {pformat(response.axon.dict())}"
                )
                rewards[idx] = -0.1  # Losing use data is unacceptable, harsh punishment

                # Update the retrieve statistics
                bt.logging.trace(f"Updating retrieve statistics for {hotkey}")
                await update_statistics(
                    ss58_address=hotkey,
                    success=False,
                    task_type="retrieve",
                    database=self.database,
                )
                continue  # skip trying to decode the data
            else:
                # Success. Reward based on miner tier
                tier_factor = await get_tier_factor(hotkey, self.database)
                rewards[idx] = 1.0 * tier_factor

            event.uids.append(uid)
            event.successful.append(success)
            event.completion_times.append(time.time() - start_time)
            event.task_status_messages.append(response.dendrite.status_message)
            event.task_status_codes.append(response.dendrite.status_code)
            event.rewards.append(rewards[idx].item())

        bt.logging.trace("Applying retrieve rewards")
        apply_reward_scores(
            self,
            uids,
            [response_tuple[0] for response_tuple in response_tuples],
            rewards,
            timeout=self.config.neuron.retrieve_timeout,
            mode="minmax",
        )

        # Determine the best UID based on rewards
        if event.rewards:
            best_index = max(range(len(event.rewards)), key=event.rewards.__getitem__)
            event.best_uid = event.uids[best_index]
            event.best_hotkey = self.metagraph.hotkeys[event.best_uid]

        return event

    async def rebalance_store(
        self,
        encrypted_data: bytes,
        uid: str,
    ):
        """
        Store encrypted data on a specific miner identified by hotkey.

        Parameters:
        - encrypted_data (bytes): The encrypted data to store.
        - uid (str): The uid of the miner where the data is to be stored.
        """
        # Prepare the synapse protocol for storing data

        g, h = setup_CRS()

        synapse = protocol.Store(
            encrypted_data=encrypted_data,
            curve=self.config.neuron.curve,
            g=ecc_point_to_hex(g),
            h=ecc_point_to_hex(h),
            seed=get_random_bytes(32).hex(),  # 256-bit seed
        )

        # Retrieve the axon for the specified miner
        axon = self.metagraph.axons[uid]

        # Send the store request to the miner
        response = await self.dendrite(
            [axon],
            synapse,
            deserialize=False,
            timeout=self.config.neuron.store_timeout,
        )

        # TODO: check if successful and error handle

    async def rebalance_retrieve(self, hotkey, data_hash, metadata):
        bt.logging.debug(
            f"rebalance_retrieve data_hash: {data_hash[:10]} | {hotkey[:10]}"
        )
        uid = self.metagraph.hotkeys.index(hotkey)
        axon = self.metagraph.axons[uid]

        synapse = protocol.Retrieve(
            data_hash=data_hash,
            seed=get_random_bytes(32).hex(),
        )

        response = await self.dendrite(
            [axon],
            synapse,
            deserialize=False,
            timeout=self.config.neuron.retrieve_timeout,
        )

        verified = False
        try:
            verified = verify_retrieve_with_seed(response[0])
            if verified:
                metadata["prev_seed"] = synapse.seed
                await update_metadata_for_data_hash(
                    hotkey, data_hash, metadata, self.database
                )

                bt.logging.trace(
                    f"Updated metadata for UID: {uid} with data: {pformat(metadata)}"
                )

            else:
                bt.logging.error(f"Failed to verify rebalance retrieve from UID: {uid}")

        except:
            bt.logging.error(f"Failed to verify rebalance retrieve from UID: {uid}")

        return response, verified

    async def rebalance_data(self, k: int):
        """
        Rebalance data storage among miners by migrating data from a set of miners to others.

        Parameters:
        - k (int): The number of miners to query and rebalance data from.

        Returns:
        - A report of the rebalancing process.
        """
        # Select k miners randomly
        source_uids = await get_available_query_miners(self, k=k)
        bt.logging.debug(f"source_uids: {source_uids}")

        # Fetch data hashes from each selected miner
        data_to_migrate = {}
        for uid in source_uids:
            hotkey = self.metagraph.hotkeys[uid]
            keys = await self.database.hkeys(f"hotkey:{hotkey}")
            if keys:
                # Select a random hash to migrate
                data_hash = random.choice(keys).decode("utf-8")
                data_to_migrate[data_hash] = hotkey

        bt.logging.debug(f"data_to_migrate: {data_to_migrate}")

        # TODO: do these as coroutines
        # Find new miners for each data hash
        for data_hash, old_hotkey in data_to_migrate.items():
            # TODO: retry with up to n miners (e.g. 3) if the miner fails to store
            # Get the encrypted data from the old miner
            metadata = await get_metadata_for_hotkey_and_hash(
                old_hotkey, data_hash, self.database
            )
            # Retrieve the data from each axon
            response, verified = await self.rebalance_retrieve(
                hotkey, data_hash, metadata
            )
            # TODO: calculate rewards for MA scores
            rewards[idx] = 0.0 if verified else -0.1

            if response.encrypted_data == None:
                continue

            if verified:
                # Store the data on a new miner
                new_uid = await get_available_query_miners(self, k=1)
                await self.rebalance_store(response.encrypted_data, new_uid[0])

        return f"Rebalanced {len(data_to_migrate)} data items."

    def run(self):
        bt.logging.info("run()")
        load_state(self)
        checkpoint(self)

        try:
            while True:
                start_epoch = time.time()

                # --- Wait until next step epoch.
                current_block = self.subtensor.get_current_block()
                while False:
                    # while self.my_subnet_uid != get_current_validtor_uid_round_robin(
                    #     self
                    # ) or (
                    #     current_block - self.prev_step_block
                    #     < self.config.neuron.blocks_per_step
                    # ):
                    bt.logging.trace(
                        f"my uid: {self.my_subnet_uid} - selected uid: {get_current_validtor_uid_round_robin(self)} - block: {ttl_get_block(self)}"
                    )
                    # --- Wait for next block.
                    time.sleep(1)
                    current_block = self.subtensor.get_current_block()

                time.sleep(2)
                if not self.wallet.hotkey.ss58_address in self.metagraph.hotkeys:
                    raise Exception(
                        f"Validator is not registered - hotkey {self.wallet.hotkey.ss58_address} not in metagraph"
                    )

                bt.logging.info(f"step({self.step}) block({ttl_get_block( self )})")

                # Run multiple forwards.
                async def run_forward():
                    coroutines = [
                        self.forward()
                        for _ in range(self.config.neuron.num_concurrent_forwards)
                    ]
                    await asyncio.gather(*coroutines)

                self.loop.run_until_complete(run_forward())

                # Resync the network state
                bt.logging.info("Checking if should checkpoint")
                if should_checkpoint(self):
                    bt.logging.info(f"Checkpointing...")
                    checkpoint(self)

                # Set the weights on chain.
                bt.logging.info(f"Checking if should set weights")
                if should_set_weights(self):
                    bt.logging.info(f"Setting weights {self.moving_averaged_scores}")
                    set_weights(self)
                    save_state(self)

                # Rollover wandb to a new run.
                if should_reinit_wandb(self):
                    bt.logging.info(f"Reinitializing wandb")
                    reinit_wandb(self)

                self.prev_step_block = ttl_get_block(self)
                if self.config.neuron.verbose:
                    bt.logging.debug(f"block at end of step: {self.prev_step_block}")
                    bt.logging.debug(f"Step took {time.time() - start_epoch} seconds")
                self.step += 1

        except Exception as err:
            bt.logging.error("Error in training loop", str(err))
            bt.logging.debug(print_exception(type(err), err, err.__traceback__))

    async def forward(self):
        bt.logging.info(f"forward step: {self.step}")

        try:
            # Store some random data
            bt.logging.info("initiating store random")
            event = await self.store_random_data()

            if self.config.neuron.verbose:
                bt.logging.debug(f"STORE EVENT LOG: {event}")

            # Log event
            log_event(self, event)

        except Exception as e:
            bt.logging.error(f"Failed to store random data: {e}")

        # Challenge every opportunity (e.g. every 2.5 blocks with 30 sec timeout)
        try:
            # Challenge some data
            bt.logging.info("initiating challenge")
            event = await self.challenge()

            if self.config.neuron.verbose:
                bt.logging.debug(f"CHALLENGE EVENT LOG: {event}")

            # Log event
            log_event(self, event)

        except Exception as e:
            bt.logging.error(f"Failed to challenge data: {e}")

        try:
            # Retrieve some data
            bt.logging.info("initiating retrieve")
            event = await self.retrieve()

            if self.config.neuron.verbose:
                bt.logging.debug(f"RETRIEVE EVENT LOG: {event}")

            # Log event
            log_event(self, event)

        except Exception as e:
            bt.logging.error(f"Failed to retrieve data: {e}")

        try:
            await self.rebalance_data(k=3)  # self.config.neuron.rebalance_k)

        except Exception as e:
            bt.logging.error(f"Failed to rebalance data {e}")

        try:
            # Update miner tiers
            bt.logging.info("Computing tiers")
            await compute_all_tiers(self.database)

            # Fetch miner statistics and usage data.
            stats = await get_miner_statistics(self.database)

            # Log all chunk hash <> hotkey pairs
            chunk_hash_map = await get_all_chunk_hashes(self.database)

            # Log the statistics and hashmap to wandb.
            if not self.config.wandb.off:
                self.wandb.log(stats)
                self.wandb.log(chunk_hash_map)

        except Exception as e:
            bt.logging.error(f"Failed to compute tiers: {e}")

        try:
            # Update the total network storage
            total_storage = await total_network_storage(self.database)
            bt.logging.info(f"Total network storage: {total_storage}")

            # Log the total storage to wandb.
            if not self.config.wandb.off:
                self.wandb.log({"total_storage": total_storage})

        except Exception as e:
            bt.logging.error(f"Failed to calculate total network storage: {e}")


def main():
    neuron().run()


if __name__ == "__main__":
    main()
