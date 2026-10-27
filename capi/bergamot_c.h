/*
 * A plain C interface to Mozilla's Bergamot translator, for callers that
 * can't use C++ (e.g. Dart's dart:ffi).
 *
 * Usage:
 *   bt_service* service; bt_model* model; char* error = NULL;
 *   bt_service_create(0, &service, &error);
 *   bt_model_load(config_yaml, "/path/to/model-dir", &model, &error);
 *   const char* in[] = {"Ci vediamo domani?"};
 *   char** out;
 *   bt_translate(service, model, NULL, in, 1, 0, &out, &error);
 *   ...  out[0] is "See you tomorrow?"
 *   bt_free_strings(out, 1); bt_model_free(model); bt_service_free(service);
 *
 * Errors: every function that can fail returns a bt_status. On failure,
 * if `error` is not NULL, *error receives a malloc'd, NUL-terminated message
 * (free it with bt_free_string); on success *error is left untouched. The
 * engine's internal fatal errors (Marian's ABORT) are reported this way too:
 * this library makes them throw instead of calling abort(), and catches
 * everything at this boundary, so a bad model file is an error, not a crash.
 *
 * Threads: calls are blocking and run on the calling thread. A service and
 * the models used with it must be used from one thread at a time; separate
 * services may run on separate threads with separate models.
 *
 * Strings are UTF-8 throughout.
 */
#ifndef BERGAMOT_C_H
#define BERGAMOT_C_H

#include <stddef.h>

#ifdef __cplusplus
extern "C" {
#endif

#if defined(_WIN32)
#  ifdef BERGAMOT_C_BUILD
#    define BT_API __declspec(dllexport)
#  else
#    define BT_API __declspec(dllimport)
#  endif
#else
#  define BT_API __attribute__((visibility("default")))
#endif

typedef enum bt_status {
  BT_OK = 0,
  /* A required pointer was NULL, or count was 0 with texts NULL. */
  BT_ERROR_INVALID_ARGUMENT = 1,
  /* The config couldn't be parsed, or a model/vocab/shortlist file is
     missing or unreadable as a model. */
  BT_ERROR_MODEL_LOAD = 2,
  /* The engine failed while translating. */
  BT_ERROR_TRANSLATION = 3,
  BT_ERROR_OUT_OF_MEMORY = 4,
  BT_ERROR_UNKNOWN = 5
} bt_status;

/* bt_translate flags. */
/* Input is HTML: tags are kept out of translation and put back around the
   translated text. */
#define BT_FLAG_HTML 1u

typedef struct bt_service bt_service;
typedef struct bt_model bt_model;

/* This library's version and the engine commit it was built from, e.g.
   "1 (bergamot v0.6.0+69455ac)". Static; never free it. */
BT_API const char* bt_version(void);

/* cache_size: translated sentences to remember (0 = no cache). */
BT_API bt_status bt_service_create(size_t cache_size, bt_service** out,
                                   char** error);
BT_API void bt_service_free(bt_service* service);

/* config_yaml: a Bergamot/Marian model config (models, vocabs, shortlist,
   beam-size, gemm-precision, ...). Relative paths in it are resolved
   against model_dir; model_dir may be NULL if every path is absolute.
   Loading reads every file into memory and can take a second or more:
   don't call it on a UI thread. */
BT_API bt_status bt_model_load(const char* config_yaml, const char* model_dir,
                               bt_model** out, char** error);
BT_API void bt_model_free(bt_model* model);

/* Translates `count` texts (each may hold several sentences or paragraphs)
   in one batch. With `pivot` not NULL, translates with `model` and then
   with `pivot` (e.g. it->en, then en->de, for it->de); `model`'s target
   language must be `pivot`'s source. On success *out receives an array of
   `count` malloc'd strings, in input order; free it with bt_free_strings.
   flags: 0 or BT_FLAG_HTML. */
BT_API bt_status bt_translate(bt_service* service, const bt_model* model,
                              const bt_model* pivot, const char* const* texts,
                              size_t count, unsigned flags, char*** out,
                              char** error);

BT_API void bt_free_strings(char** strings, size_t count);
BT_API void bt_free_string(char* string);

#ifdef __cplusplus
}
#endif

#endif /* BERGAMOT_C_H */
