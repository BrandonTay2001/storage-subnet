import os
import json
import base64
import hashlib
import binascii
from collections import defaultdict
from typing import Dict, List, Any

import Crypto
from Crypto.Random import random
from Crypto.Cipher import AES
from Crypto.Random import get_random_bytes

from .ecc import hex_to_ecc_point, ecc_point_to_hex, hash_data, ECCommitment
from .merkle import MerkleTree


def make_random_file(name=None, maxsize=1024):
    size = random.randint(128, maxsize)
    data = os.urandom(size)
    if isinstance(name, str):
        with open(name, "wb") as fout:
            fout.write(data)
    else:
        return data


# Determine a random chunksize between 2kb-128kb (random sample from this range) store as chunksize_E
def get_random_chunksize(maxsize=128):
    return random.randint(2, maxsize)


def chunk_data(data, chunksize: int):
    for i in range(0, len(data), chunksize):
        yield data[i : i + chunksize]


def is_hex_str(s):
    """
    Check if the input string is a valid hexadecimal string.

    :param s: The string to check
    :return: True if s is a valid hexadecimal string, False otherwise
    """
    # A valid hex string must have an even number of characters
    if len(s) % 2 != 0:
        return False

    # Check if each character is a valid hex character
    try:
        int(s, 16)
        return True
    except ValueError:
        return False


def encrypt_data(filename, key):
    """
    Encrypt the data in the given filename using AES-GCM.

    Parameters:
    - filename: str or bytes. If str, it's considered as a file name. If bytes, as the data itself.
    - key: bytes. 16-byte (128-bit), 24-byte (192-bit), or 32-byte (256-bit) secret key.

    Returns:
    - cipher_text: bytes. The encrypted data.
    - nonce: bytes. The nonce used for the GCM mode.
    - tag: bytes. The tag for authentication.
    """

    # If filename is a string, treat it as a file name and read the data
    if isinstance(filename, str):
        with open(filename, "rb") as file:
            data = file.read()
    else:
        data = filename

    # Initialize AES-GCM cipher
    cipher = AES.new(key, AES.MODE_GCM)

    # Encrypt the data
    cipher_text, tag = cipher.encrypt_and_digest(data)

    return cipher_text, cipher.nonce, tag


def serialize_dict_with_bytes(commitments: Dict[int, Dict[str, Any]]) -> str:
    # Convert our custom objects to serializable objects
    for commitment in commitments:
        # Check if 'point' is a bytes-like object, if not, it's already a string (hex)
        if isinstance(commitment.get("point"), bytes):
            commitment["point"] = commitment["point"].hex()

        if commitment.get("data_chunk"):
            commitment["data_chunk"] = commitment["data_chunk"].hex()

        # Similarly, check for 'merkle_proof' and convert if necessary
        if commitment.get("merkle_proof"):
            serialized_merkle_proof = []
            for proof in commitment["merkle_proof"]:
                serialized_proof = {}
                for side, value in proof.items():
                    # Check if value is a bytes-like object, if not, it's already a string (hex)
                    if isinstance(value, bytes):
                        serialized_proof[side] = value.hex()
                    else:
                        serialized_proof[side] = value
                serialized_merkle_proof.append(serialized_proof)
            commitment["merkle_proof"] = serialized_merkle_proof

        # Randomness is an integer and should be safely converted to string without checking type
        if commitment.get("randomness"):
            commitment["randomness"] = str(commitment["randomness"])

    # Convert the entire structure to JSON
    return json.dumps(commitments)


# Deserializer function
def deserialize_dict_with_bytes(serialized: str) -> Dict[int, Dict[str, Any]]:
    def hex_to_bytes(hex_str: str) -> bytes:
        return bytes.fromhex(hex_str)

    def deserialize_helper(d: Dict[str, Any]) -> Dict[str, Any]:
        for key, value in d.items():
            if key == "data_chunk":
                d[key] = hex_to_bytes(value)
            elif key == "randomness":
                d[key] = int(value)
            elif key == "merkle_proof" and value is not None:
                d[key] = [{k: v for k, v in item.items()} for item in value]
        return d

    # Parse the JSON string back to a dictionary
    return json.loads(serialized, object_hook=deserialize_helper)


def decode_commitments(encoded_commitments):
    decoded_commitments = base64.b64decode(encoded_commitments)
    commitments = deserialize_dict_with_bytes(decoded_commitments)
    return commitments


def decode_storage(encoded_storage):
    decoded_storage = base64.b64decode(encoded_storage).decode("utf-8")
    dict_storage = json.loads(decoded_storage)
    dict_storage["commitments"] = decode_commitments(dict_storage["commitments"])
    dict_storage["params"] = json.loads(
        base64.b64decode(dict_storage["params"]).decode("utf-8")
    )
    return dict_storage


def b64_encode(data):
    if isinstance(data, list) and isinstance(data[0], bytes):
        data = [d.hex() for d in data]
    if isinstance(data, dict) and isinstance(data[list(data.keys())[0]], bytes):
        data = {k: v.hex() for k, v in data.items()}
    return base64.b64encode(json.dumps(data).encode()).decode("utf-8")


def b64_decode(data, decode_hex=False):
    data = data.decode("utf-8") if isinstance(data, bytes) else data
    decoded_data = json.loads(base64.b64decode(data).decode("utf-8"))
    if decode_hex:
        try:
            decoded_data = (
                [bytes.fromhex(d) for d in decoded_data]
                if isinstance(decoded_data, list)
                else {k: bytes.fromhex(v) for k, v in decoded_data.items()}
            )
        except:
            pass
    return decoded_data


def encode_miner_storage(**kwargs):
    randomness = kwargs.get("randomness")
    chunks = kwargs.get("data_chunks")
    points = kwargs.get("commitments")
    points = [
        ecc_point_to_hex(p)
        for p in points
        if isinstance(p, Crypto.PublicKey.ECC.EccPoint)
    ]
    merkle_tree = kwargs.get("merkle_tree")

    # store (randomness values, merkle tree, commitments, data chunks)
    miner_store = {
        "randomness": b64_encode(randomness),
        "data_chunks": b64_encode(chunks),
        "commitments": b64_encode(points),
        "merkle_tree": b64_encode(merkle_tree.serialize()),
    }
    return json.dumps(miner_store).encode()


def decode_miner_storage(encoded_storage, curve):
    xy = json.loads(encoded_storage.decode("utf-8"))
    xz = {
        k: b64_decode(v, decode_hex=True if k != "commitments" else False)
        for k, v in xy.items()
    }
    xz["commitments"] = [hex_to_ecc_point(c, curve) for c in xz["commitments"]]
    xz["merkle_tree"] = MerkleTree().deserialize(xz["merkle_tree"])
    return xz


def GetSynapse(config):
    # Setup CRS for this round of validation
    g, h = setup_CRS(curve=config.curve)

    # Make a random bytes file to test the miner
    random_data = make_random_file(maxsize=config.maxsize)

    # Random encryption key for now (never will decrypt)
    key = get_random_bytes(32)  # 256-bit key

    # Encrypt the data
    encrypted_data, nonce, tag = encrypt_data(
        random_data,
        key,  # TODO: Use validator key as the encryption key?
    )

    # Convert to base64 for compactness
    b64_encrypted_data = base64.b64encode(encrypted_data).decode("utf-8")

    # Hash the encrypted data
    data_hash = hash_data(encrypted_data)

    # Chunk the data
    chunk_size = get_random_chunksize()
    # chunks = list(chunk_data(encrypted_data, chunksize))

    syn = synapse = protocol.Store(
        chunk_size=chunk_size,
        encrypted_data=b64_encrypted_data,
        data_hash=data_hash,
        curve=config.curve,
        g=ecc_point_to_hex(g),
        h=ecc_point_to_hex(h),
        size=sys.getsizeof(encrypted_data),
    )
    return synapse


def validate_merkle_proof(proof, target_hash, merkle_root):
    merkle_root = bytearray.fromhex(merkle_root)
    target_hash = bytearray.fromhex(target_hash)
    if len(proof) == 0:
        return target_hash == merkle_root
    else:
        proof_hash = target_hash
        for p in proof:
            try:
                # the sibling is a left node
                sibling = bytearray.fromhex(p["left"])
                proof_hash = hashlib.sha3_256(sibling + proof_hash).digest()
            except:
                # the sibling is a right node
                sibling = bytearray.fromhex(p["right"])
                proof_hash = hashlib.sha3_256(proof_hash + sibling).digest()
        return proof_hash == merkle_root


def verify_challenge(synapse):
    # TODO: Add checks and defensive programming here to handle all types
    # (bytes, str, hex, ecc point, etc)
    committer = ECCommitment(
        hex_to_ecc_point(synapse.g, synapse.curve),
        hex_to_ecc_point(synapse.h, synapse.curve),
    )
    commitment = hex_to_ecc_point(synapse.commitment, synapse.curve)

    if not committer.open(
        commitment, hash_data(synapse.data_chunk), synapse.random_value
    ):
        print(f"Opening commitment failed")
        return False

    if not validate_merkle_proof(
        synapse.merkle_proof, ecc_point_to_hex(commitment), synapse.merkle_root
    ):
        print(f"Merkle proof validation failed")
        return False

    return True
