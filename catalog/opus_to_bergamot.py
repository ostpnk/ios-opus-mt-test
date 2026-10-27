"""Re-indexes an OPUS-MT Marian model so Bergamot can drive it.

OPUS-MT models use one joint vocab.yml (piece -> id) and a single tied
embedding matrix (`Wemb`) shared by encoder, decoder and output layer.
Bergamot instead tokenizes with the SentencePiece models themselves, so it
feeds SentencePiece ids: the source side numbered by source.spm, the
target side by target.spm. This builds a model in that numbering:

  encoder_Wemb[i]          = Wemb[yml_id(source piece i)]
  decoder_Wemb[i]          = Wemb[yml_id(target piece i)]   (tied to output)
  decoder_ff_logit_out_b   = bias reordered the same way as decoder_Wemb

and rewrites special:model.yml to untie the source embedding
(tied-embeddings-all: false, tied-embeddings: true, dim-vocabs: per side).
A SentencePiece piece missing from vocab.yml (normally only <s>) gets a
zero embedding and a very negative output bias so it is never generated.

Usage: python3 -I opus_to_bergamot.py MODEL.npz VOCAB.yml SOURCE.spm TARGET.spm OUT.npz
"""

import io
import sys
import zipfile

import numpy as np
import yaml


def _varint(data, pos):
    shift = result = 0
    while True:
        b = data[pos]
        pos += 1
        result |= (b & 0x7F) << shift
        if not b & 0x80:
            return result, pos
        shift += 7


def spm_pieces(path):
    """The pieces of a SentencePiece model, in id order — field 1 of
    ModelProto (repeated SentencePiece), whose field 1 is the piece. A
    minimal protobuf reader: only what's needed, skipping everything else."""
    data = open(path, "rb").read()
    pieces = []
    pos = 0
    while pos < len(data):
        key, pos = _varint(data, pos)
        field, wire = key >> 3, key & 7
        if wire == 0:
            _, pos = _varint(data, pos)
        elif wire == 1:
            pos += 8
        elif wire == 5:
            pos += 4
        elif wire == 2:
            length, pos = _varint(data, pos)
            if field == 1:
                entry, epos = data[pos:pos + length], 0
                while epos < len(entry):
                    ekey, epos = _varint(entry, epos)
                    efield, ewire = ekey >> 3, ekey & 7
                    if ewire == 2:
                        elen, epos = _varint(entry, epos)
                        if efield == 1:
                            pieces.append(entry[epos:epos + elen].decode("utf-8"))
                        epos += elen
                    elif ewire == 0:
                        _, epos = _varint(entry, epos)
                    elif ewire == 5:
                        epos += 4
                    elif ewire == 1:
                        epos += 8
                    else:
                        raise ValueError(f"{path}: unsupported wire type {ewire}")
            pos += length
        else:
            raise ValueError(f"{path}: unsupported wire type {wire}")
    return pieces


def convert(model_path, vocab_path, source_spm, target_spm, out_path, log=print):
    with open(vocab_path, encoding="utf-8") as f:
        # BaseLoader: every key stays a plain string. safe_load would
        # resolve pieces like `1` or `yes` to other types, and fails on a
        # `<<` piece (YAML's merge key).
        joint = yaml.load(f, Loader=yaml.BaseLoader)
    joint = {k: int(v) for k, v in joint.items()}

    model = dict(np.load(model_path))
    wemb = model.pop("Wemb")
    bias = model.pop("decoder_ff_logit_out_b")

    def reindex(names, side):
        rows = np.zeros((len(names), wemb.shape[1]), dtype=wemb.dtype)
        b = np.full((1, len(names)), -1e4, dtype=bias.dtype)
        missing = []
        for i, piece in enumerate(names):
            j = joint.get(piece)
            if j is None:
                missing.append(piece)
                continue
            rows[i] = wemb[j]
            b[0, i] = bias[0, j]
        log(f"{side}: {len(names)} pieces, {len(missing)} not in vocab.yml: {missing[:10]}")
        return rows, b

    model["encoder_Wemb"], _ = reindex(spm_pieces(source_spm), "source")
    model["decoder_Wemb"], model["decoder_ff_logit_out_b"] = reindex(
        spm_pieces(target_spm), "target"
    )

    raw = model.pop("special:model.yml")
    cfg = yaml.safe_load(raw.tobytes().decode("utf-8").rstrip("\x00"))
    if not cfg.get("tied-embeddings-all"):
        raise ValueError(f"{model_path}: expected tied-embeddings-all (OPUS-MT's layout)")
    cfg["tied-embeddings-all"] = False
    cfg["tied-embeddings"] = True
    cfg["tied-embeddings-src"] = False
    cfg["dim-vocabs"] = [model["encoder_Wemb"].shape[0], model["decoder_Wemb"].shape[0]]
    text = yaml.safe_dump(cfg, sort_keys=True).encode("utf-8") + b"\x00"
    model["special:model.yml"] = np.frombuffer(text, dtype=np.int8)

    # Not np.savez: it streams each member (sizes in a trailing data
    # descriptor, ZIP64 forced), which Marian's cnpy reader can't follow
    # ("npz_load: failed fread"). writestr puts the sizes in the local
    # header, as cnpy expects.
    with zipfile.ZipFile(out_path, "w", zipfile.ZIP_STORED) as z:
        for name, array in model.items():
            buf = io.BytesIO()
            np.lib.format.write_array(buf, np.ascontiguousarray(array))
            z.writestr(name + ".npy", buf.getvalue())
    log(f"wrote {out_path}")


if __name__ == "__main__":
    if len(sys.argv) != 6:
        sys.exit(__doc__)
    convert(*sys.argv[1:])
