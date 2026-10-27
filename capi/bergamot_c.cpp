// Implementation of bergamot_c.h over Bergamot's BlockingService.

#include "bergamot_c.h"

#include <cstdlib>
#include <cstring>
#include <memory>
#include <mutex>
#include <new>
#include <stdexcept>
#include <string>
#include <utility>
#include <vector>

#include "common/logging.h"
#include "translator/parser.h"
#include "translator/response_options.h"
#include "translator/service.h"
#include "translator/translation_model.h"

#ifndef BT_ENGINE_VERSION
#define BT_ENGINE_VERSION "unknown"
#endif

namespace bergamot = marian::bergamot;

struct bt_service {
  std::unique_ptr<bergamot::BlockingService> service;
};

struct bt_model {
  std::shared_ptr<bergamot::TranslationModel> model;
};

namespace {

// Marian's ABORT() calls std::abort() unless told to throw; a library
// mustn't take its host process down over a bad model file. Process-wide,
// and set before any engine code runs.
void throwOnAbort() {
  static std::once_flag once;
  std::call_once(once, [] { marian::setThrowExceptionOnAbort(true); });
}

char* copyString(const std::string& s) {
  char* out = static_cast<char*>(std::malloc(s.size() + 1));
  if (out == nullptr) throw std::bad_alloc();
  std::memcpy(out, s.data(), s.size() + 1);
  return out;
}

void setError(char** error, const char* message) {
  if (error == nullptr) return;
  // Best effort: if even the message can't be allocated, report none.
  size_t n = std::strlen(message);
  char* out = static_cast<char*>(std::malloc(n + 1));
  if (out != nullptr) std::memcpy(out, message, n + 1);
  *error = out;
}

// Runs `body`, mapping any exception to a status: bad_alloc to
// BT_ERROR_OUT_OF_MEMORY, any other exception (Marian's
// MarianRuntimeException from ABORT included) to `failure`.
template <class Body>
bt_status guarded(bt_status failure, char** error, Body&& body) {
  try {
    throwOnAbort();
    body();
    return BT_OK;
  } catch (const std::bad_alloc&) {
    setError(error, "out of memory");
    return BT_ERROR_OUT_OF_MEMORY;
  } catch (const std::exception& e) {
    setError(error, e.what());
    return failure;
  } catch (...) {
    setError(error, "unknown error");
    return BT_ERROR_UNKNOWN;
  }
}

}  // namespace

extern "C" {

const char* bt_version(void) { return "1 (bergamot " BT_ENGINE_VERSION ")"; }

bt_status bt_service_create(size_t cache_size, bt_service** out, char** error) {
  if (out == nullptr) {
    setError(error, "out is NULL");
    return BT_ERROR_INVALID_ARGUMENT;
  }
  return guarded(BT_ERROR_UNKNOWN, error, [&] {
    bergamot::BlockingService::Config config;
    config.cacheSize = cache_size;
    config.logger.level = "off";
    auto service = std::make_unique<bt_service>();
    service->service = std::make_unique<bergamot::BlockingService>(config);
    *out = service.release();
  });
}

void bt_service_free(bt_service* service) { delete service; }

bt_status bt_model_load(const char* config_yaml, const char* model_dir, bt_model** out,
                        char** error) {
  if (config_yaml == nullptr || out == nullptr) {
    setError(error, config_yaml == nullptr ? "config_yaml is NULL" : "out is NULL");
    return BT_ERROR_INVALID_ARGUMENT;
  }
  return guarded(BT_ERROR_MODEL_LOAD, error, [&] {
    // parseOptionsFromString resolves relative paths against the directory
    // of the path it's given, so name a (never read) file inside model_dir.
    std::string pathsRelativeTo;
    if (model_dir != nullptr && model_dir[0] != '\0') {
      pathsRelativeTo = std::string(model_dir) + "/config.yml";
    }
    auto options = bergamot::parseOptionsFromString(config_yaml, /*validate=*/false, pathsRelativeTo);
    auto model = std::make_unique<bt_model>();
    model->model = std::make_shared<bergamot::TranslationModel>(options);
    *out = model.release();
  });
}

void bt_model_free(bt_model* model) { delete model; }

bt_status bt_translate(bt_service* service, const bt_model* model, const bt_model* pivot,
                       const char* const* texts, size_t count, unsigned flags, char*** out,
                       char** error) {
  if (service == nullptr || model == nullptr || out == nullptr || (texts == nullptr && count > 0)) {
    setError(error, "service, model, texts or out is NULL");
    return BT_ERROR_INVALID_ARGUMENT;
  }
  for (size_t i = 0; i < count; ++i) {
    if (texts[i] == nullptr) {
      setError(error, "a text is NULL");
      return BT_ERROR_INVALID_ARGUMENT;
    }
  }
  // Allocated outside guarded() so a failure part-way can free what's done.
  char** results = nullptr;
  size_t filled = 0;
  bt_status status = guarded(BT_ERROR_TRANSLATION, error, [&] {
    std::vector<std::string> sources(texts, texts + count);
    bergamot::ResponseOptions options;
    options.HTML = (flags & BT_FLAG_HTML) != 0;
    std::vector<bergamot::ResponseOptions> perText(count, options);

    std::vector<bergamot::Response> responses =
        pivot == nullptr
            ? service->service->translateMultiple(model->model, std::move(sources), perText)
            : service->service->pivotMultiple(model->model, pivot->model, std::move(sources), perText);

    if (responses.size() != count) {
      throw std::runtime_error("engine returned " + std::to_string(responses.size()) +
                               " translations for " + std::to_string(count) + " texts");
    }
    // calloc (not malloc) so bt_free_strings is safe on a part-filled array.
    results = static_cast<char**>(std::calloc(count == 0 ? 1 : count, sizeof(char*)));
    if (results == nullptr) throw std::bad_alloc();
    for (; filled < count; ++filled) {
      results[filled] = copyString(responses[filled].target.text);
    }
  });
  if (status != BT_OK) {
    bt_free_strings(results, filled);
    return status;
  }
  *out = results;
  return BT_OK;
}

void bt_free_strings(char** strings, size_t count) {
  if (strings == nullptr) return;
  for (size_t i = 0; i < count; ++i) std::free(strings[i]);
  std::free(strings);
}

void bt_free_string(char* string) { std::free(string); }

}  // extern "C"
