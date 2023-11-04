import json
import hashlib
import binascii


class MerkleTree(object):
    def __init__(self, hash_type="sha3_256"):
        hash_type = hash_type.lower()
        if hash_type in ["sha3_256"]:
            self.hash_function = getattr(hashlib, hash_type)
        else:
            raise Exception("`hash_type` {} nor supported".format(hash_type))

        self.reset_tree()

    def _to_hex(self, x):
        try:  # python3
            return x.hex()
        except:  # python2
            return binascii.hexlify(x)

    def reset_tree(self):
        self.leaves = list()
        self.levels = None
        self.is_ready = False

    def add_leaf(self, values, do_hash=False):
        self.is_ready = False
        # check if single leaf
        if not isinstance(values, tuple) and not isinstance(values, list):
            values = [values]
        for v in values:
            if do_hash:
                v = v.encode("utf-8")
                v = self.hash_function(v).hexdigest()
            v = bytearray.fromhex(v)
            self.leaves.append(v)

    def get_leaf(self, index):
        return self._to_hex(self.leaves[index])

    def get_leaf_count(self):
        return len(self.leaves)

    def get_tree_ready_state(self):
        return self.is_ready

    def _calculate_next_level(self):
        solo_leave = None
        N = len(self.levels[0])  # number of leaves on the level
        if N % 2 == 1:  # if odd number of leaves on the level
            solo_leave = self.levels[0][-1]
            N -= 1

        new_level = []
        for l, r in zip(self.levels[0][0:N:2], self.levels[0][1:N:2]):
            new_level.append(self.hash_function(l + r).digest())
        if solo_leave is not None:
            new_level.append(solo_leave)
        self.levels = [
            new_level,
        ] + self.levels  # prepend new level

    def make_tree(self):
        self.is_ready = False
        if self.get_leaf_count() > 0:
            self.levels = [
                self.leaves,
            ]
            while len(self.levels[0]) > 1:
                self._calculate_next_level()
        self.is_ready = True

    def get_merkle_root(self):
        if self.is_ready:
            if self.levels is not None:
                return self._to_hex(self.levels[0][0])
            else:
                return None
        else:
            return None

    def get_proof(self, index):
        if self.levels is None:
            return None
        elif not self.is_ready or index > len(self.leaves) - 1 or index < 0:
            return None
        else:
            proof = []
            for x in range(len(self.levels) - 1, 0, -1):
                level_len = len(self.levels[x])
                if (index == level_len - 1) and (
                    level_len % 2 == 1
                ):  # skip if this is an odd end node
                    index = int(index / 2.0)
                    continue
                is_right_node = index % 2
                sibling_index = index - 1 if is_right_node else index + 1
                sibling_pos = "left" if is_right_node else "right"
                sibling_value = self._to_hex(self.levels[x][sibling_index])
                proof.append({sibling_pos: sibling_value})
                index = int(index / 2.0)
            return proof

    def update_leaf(self, index, new_value):
        """Update a specific leaf in the tree and propagate changes upwards."""
        if not self.is_ready:
            return None
        new_value = bytearray.fromhex(new_value)
        self.levels[-1][index] = new_value
        for x in range(len(self.levels) - 1, 0, -1):
            parent_index = index // 2
            left_child = self.levels[x][parent_index * 2]
            try:
                right_child = self.levels[x][parent_index * 2 + 1]
            except IndexError:
                right_child = bytearray()
            self.levels[x - 1][parent_index] = self.hash_function(
                left_child + right_child
            ).digest()
            index = parent_index

    def serialize(self):
        """
        Serializes the MerkleTree object into a JSON string.
        """
        # Convert the bytearray leaves and levels to hex strings for serialization
        leaves = [self._to_hex(leaf) for leaf in self.leaves]
        levels = None
        if self.levels is not None:
            levels = []
            for level in self.levels:
                levels.append([self._to_hex(item) for item in level])

        # Construct a dictionary with the MerkleTree properties
        merkle_tree_data = {
            "leaves": leaves,
            "levels": levels,
            "is_ready": self.is_ready,
        }

        # Convert the dictionary to a JSON string
        return json.dumps(merkle_tree_data)

    @classmethod
    def deserialize(cls, json_data, hash_type="sha3_256"):
        """
        Deserializes the JSON string into a MerkleTree object.
        """
        # Convert the JSON string back to a dictionary
        merkle_tree_data = json.loads(json_data)

        # Create a new MerkleTree object
        m_tree = cls(hash_type)

        # Convert the hex strings back to bytearrays and set the leaves and levels
        m_tree.leaves = [bytearray.fromhex(leaf) for leaf in merkle_tree_data["leaves"]]
        if merkle_tree_data["levels"] is not None:
            m_tree.levels = []
            for level in merkle_tree_data["levels"]:
                m_tree.levels.append([bytearray.fromhex(item) for item in level])
        m_tree.is_ready = merkle_tree_data["is_ready"]

        return m_tree


def validate_merkle_proof(proof, target_hash, merkle_root, hash_type="sha3_256"):
    hash_func = getattr(hashlib, hash_type)
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
                proof_hash = hash_func(sibling + proof_hash).digest()
            except:
                # the sibling is a right node
                sibling = bytearray.fromhex(p["right"])
                proof_hash = hash_func(proof_hash + sibling).digest()
        return proof_hash == merkle_root


class MerkleTreeSerializer:
    @staticmethod
    def serialize(m_tree):
        """
        Serializes the MerkleTree object into a JSON string.
        """
        if not isinstance(m_tree, MerkleTree):
            raise ValueError("m_tree must be an instance of MerkleTree")

        # Convert the bytearray leaves and levels to hex strings for serialization
        leaves = [m_tree._to_hex(leaf) for leaf in m_tree.leaves]
        levels = None
        if m_tree.levels is not None:
            levels = []
            for level in m_tree.levels:
                levels.append([m_tree._to_hex(item) for item in level])

        # Construct a dictionary with the MerkleTree properties
        merkle_tree_data = {
            "leaves": leaves,
            "levels": levels,
            "is_ready": m_tree.is_ready,
        }

        # Convert the dictionary to a JSON string
        return json.dumps(merkle_tree_data)

    @staticmethod
    def deserialize(json_data, hash_type="sha3_256"):
        """
        Deserializes the JSON string into a MerkleTree object.
        """
        # Convert the JSON string back to a dictionary
        merkle_tree_data = json.loads(json_data)

        # Create a new MerkleTree object
        m_tree = MerkleTree(hash_type)

        # Convert the hex strings back to bytearrays and set the leaves and levels
        m_tree.leaves = [bytearray.fromhex(leaf) for leaf in merkle_tree_data["leaves"]]
        if merkle_tree_data["levels"] is not None:
            m_tree.levels = []
            for level in merkle_tree_data["levels"]:
                m_tree.levels.append([bytearray.fromhex(item) for item in level])
        m_tree.is_ready = merkle_tree_data["is_ready"]

        return m_tree


if False:
    # Usage Example:
    # Given a MerkleTree instance 'm_tree'
    m_tree = MerkleTree()
    m_tree.add_leaf(["leaf1", "leaf2"], do_hash=True)
    m_tree.make_tree()

    # Serialize the MerkleTree
    serialized_tree = MerkleTreeSerializer.serialize(m_tree)

    # Later on, deserialize back into a MerkleTree object
    deserialized_tree = MerkleTreeSerializer.deserialize(serialized_tree)
