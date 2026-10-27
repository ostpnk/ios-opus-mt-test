"""libbergamot (capi/bergamot_c.h) from Python, through ctypes — what the
catalog scripts translate with, so a bundle is checked through the same C
interface apps use.

    with Bergamot("out/linux/lib/libbergamot.so") as bt:
        model = bt.load_model(config_yaml, "/path/to/model-dir")
        print(bt.translate(model, ["Ci vediamo domani?"]))
"""

import ctypes

OK = 0


class BergamotError(RuntimeError):
    def __init__(self, status, message):
        super().__init__(f"bt_status {status}: {message}")
        self.status = status


class Bergamot:
    def __init__(self, library_path, cache_size=0):
        lib = ctypes.CDLL(library_path)
        p = ctypes.c_void_p
        pp = ctypes.POINTER(ctypes.c_void_p)
        err = ctypes.POINTER(ctypes.c_char_p)
        lib.bt_version.restype = ctypes.c_char_p
        lib.bt_service_create.argtypes = [ctypes.c_size_t, pp, ctypes.POINTER(p)]
        lib.bt_service_free.argtypes = [p]
        lib.bt_model_load.argtypes = [ctypes.c_char_p, ctypes.c_char_p, pp, ctypes.POINTER(p)]
        lib.bt_model_free.argtypes = [p]
        lib.bt_translate.argtypes = [
            p, p, p,
            ctypes.POINTER(ctypes.c_char_p), ctypes.c_size_t, ctypes.c_uint,
            ctypes.POINTER(ctypes.POINTER(p)), ctypes.POINTER(p),
        ]
        lib.bt_free_strings.argtypes = [ctypes.POINTER(p), ctypes.c_size_t]
        lib.bt_free_string.argtypes = [p]
        self._lib = lib
        self._models = []
        service = p()
        self._check(lib.bt_service_create(cache_size, ctypes.byref(service), None), None)
        self._service = service

    @property
    def version(self):
        return self._lib.bt_version().decode()

    def _check(self, status, error):
        if status == OK:
            return
        message = ""
        if error is not None and error.value:
            message = ctypes.cast(error, ctypes.c_char_p).value.decode(errors="replace")
            self._lib.bt_free_string(error)
        raise BergamotError(status, message)

    def load_model(self, config_yaml, model_dir):
        model = ctypes.c_void_p()
        error = ctypes.c_void_p()
        status = self._lib.bt_model_load(
            config_yaml.encode(), model_dir.encode(), ctypes.byref(model), ctypes.byref(error)
        )
        self._check(status, error)
        self._models.append(model)
        return model

    def free_model(self, model):
        self._models.remove(model)
        self._lib.bt_model_free(model)

    def translate(self, model, texts, pivot=None, html=False):
        if not texts:
            return []
        inputs = (ctypes.c_char_p * len(texts))(*[t.encode() for t in texts])
        out = ctypes.POINTER(ctypes.c_void_p)()
        error = ctypes.c_void_p()
        status = self._lib.bt_translate(
            self._service, model, pivot, inputs, len(texts), 1 if html else 0,
            ctypes.byref(out), ctypes.byref(error),
        )
        self._check(status, error)
        try:
            return [
                ctypes.cast(out[i], ctypes.c_char_p).value.decode()
                for i in range(len(texts))
            ]
        finally:
            self._lib.bt_free_strings(out, len(texts))

    def close(self):
        for model in self._models:
            self._lib.bt_model_free(model)
        self._models.clear()
        if self._service:
            self._lib.bt_service_free(self._service)
            self._service = None

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()
