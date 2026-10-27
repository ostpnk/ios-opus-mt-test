/*
 * Tests libbergamot through its C interface only (this file is C, so it
 * also proves bergamot_c.h is plain C).
 *
 * Usage: test_capi <it-en model dir> <sentences file> [<en-it model dir>]
 * Each model dir holds a config.yml whose paths are relative to it.
 * Exits non-zero if any check fails.
 */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>

#include "bergamot_c.h"

static int failures = 0;

#define CHECK(cond, ...)                         \
  do {                                           \
    if (!(cond)) {                               \
      printf("FAIL %s:%d: ", __FILE__, __LINE__); \
      printf(__VA_ARGS__);                       \
      printf("\n");                              \
      failures++;                                \
    }                                            \
  } while (0)

static double now_ms(void) {
  struct timespec ts;
  clock_gettime(CLOCK_MONOTONIC, &ts);
  return ts.tv_sec * 1000.0 + ts.tv_nsec / 1e6;
}

static char* read_file(const char* path) {
  FILE* f = fopen(path, "rb");
  if (!f) return NULL;
  fseek(f, 0, SEEK_END);
  long n = ftell(f);
  fseek(f, 0, SEEK_SET);
  char* buf = malloc((size_t)n + 1);
  if (buf && fread(buf, 1, (size_t)n, f) != (size_t)n) { free(buf); buf = NULL; }
  if (buf) buf[n] = '\0';
  fclose(f);
  return buf;
}

static bt_model* load(const char* dir) {
  char path[4096];
  snprintf(path, sizeof path, "%s/config.yml", dir);
  char* config = read_file(path);
  CHECK(config != NULL, "can't read %s", path);
  if (!config) return NULL;
  bt_model* model = NULL;
  char* error = NULL;
  double t = now_ms();
  bt_status s = bt_model_load(config, dir, &model, &error);
  printf("load %s: %.0f ms\n", dir, now_ms() - t);
  CHECK(s == BT_OK, "bt_model_load(%s) = %d: %s", dir, s, error ? error : "");
  bt_free_string(error);
  free(config);
  return model;
}

/* Splits `text` into lines in place; returns the number of non-empty lines. */
static size_t split_lines(char* text, const char** lines, size_t max) {
  size_t n = 0;
  for (char* line = strtok(text, "\n"); line && n < max; line = strtok(NULL, "\n")) {
    if (line[0] != '\0') lines[n++] = line;
  }
  return n;
}

int main(int argc, char** argv) {
  if (argc < 3) {
    fprintf(stderr, "usage: %s <it-en dir> <sentences> [<en-it dir>]\n", argv[0]);
    return 2;
  }
  printf("version: %s\n", bt_version());

  bt_service* service = NULL;
  char* error = NULL;
  CHECK(bt_service_create(0, &service, &error) == BT_OK, "bt_service_create: %s", error ? error : "");
  bt_free_string(error);
  error = NULL;

  /* --- errors are reported, never fatal --- */
  bt_model* bad = NULL;
  bt_status s = bt_model_load(NULL, NULL, &bad, &error);
  CHECK(s == BT_ERROR_INVALID_ARGUMENT, "NULL config: %d", s);
  bt_free_string(error);
  error = NULL;

  s = bt_model_load("models:\n  - does-not-exist.bin\nvocabs:\n  - nope.spm\n  - nope.spm\n",
                    "/nonexistent", &bad, &error);
  CHECK(s == BT_ERROR_MODEL_LOAD, "missing files: %d", s);
  CHECK(error != NULL && error[0] != '\0', "missing files: no message");
  printf("missing files -> %d: %.120s\n", s, error ? error : "");
  bt_free_string(error);
  error = NULL;

  s = bt_model_load("models: [unclosed\n", NULL, &bad, &error);
  CHECK(s == BT_ERROR_MODEL_LOAD, "malformed YAML: %d", s);
  printf("malformed YAML -> %d: %.120s\n", s, error ? error : "");
  bt_free_string(error);
  error = NULL;

  /* --- translation --- */
  bt_model* iten = load(argv[1]);
  char* text = read_file(argv[2]);
  CHECK(text != NULL, "can't read %s", argv[2]);
  if (!iten || !text) return 1;

  const char* lines[256];
  size_t n = split_lines(text, lines, 256);
  char** out = NULL;
  double t = now_ms();
  s = bt_translate(service, iten, NULL, lines, n, 0, &out, &error);
  double ms = now_ms() - t;
  CHECK(s == BT_OK, "bt_translate = %d: %s", s, error ? error : "");
  bt_free_string(error);
  error = NULL;
  if (s == BT_OK) {
    for (size_t i = 0; i < n; i++) {
      printf("  %s\n    -> %s\n", lines[i], out[i]);
      CHECK(out[i][0] != '\0', "empty translation for: %s", lines[i]);
    }
    printf("%zu sentences in one batch: %.0f ms (%.1f ms each)\n", n, ms, ms / n);
    bt_free_strings(out, n);
  }

  s = bt_translate(service, iten, NULL, NULL, 0, 0, &out, &error);
  CHECK(s == BT_OK, "empty batch: %d", s);
  if (s == BT_OK) bt_free_strings(out, 0);

  s = bt_translate(NULL, iten, NULL, lines, 1, 0, &out, &error);
  CHECK(s == BT_ERROR_INVALID_ARGUMENT, "NULL service: %d", s);
  bt_free_string(error);
  error = NULL;

  const char* html[] = {"<p>Ci vediamo <b>domani</b>?</p>"};
  s = bt_translate(service, iten, NULL, html, 1, BT_FLAG_HTML, &out, &error);
  CHECK(s == BT_OK, "HTML: %d: %s", s, error ? error : "");
  if (s == BT_OK) {
    printf("HTML: %s\n   -> %s\n", html[0], out[0]);
    CHECK(strstr(out[0], "<b>") != NULL && strstr(out[0], "</p>") != NULL, "HTML tags lost: %s", out[0]);
    bt_free_strings(out, 1);
  }
  bt_free_string(error);
  error = NULL;

  /* --- pivot: it -> en -> it, a round trip through the second model --- */
  if (argc > 3) {
    bt_model* enit = load(argv[3]);
    if (enit) {
      const char* in[] = {"Ho fame, mangiamo qualcosa?", "Se fossi in te, non ci andrei."};
      char** direct = NULL;
      s = bt_translate(service, iten, NULL, in, 2, 0, &direct, &error);
      CHECK(s == BT_OK, "direct: %d", s);
      t = now_ms();
      s = bt_translate(service, iten, enit, in, 2, 0, &out, &error);
      ms = now_ms() - t;
      CHECK(s == BT_OK, "pivot: %d: %s", s, error ? error : "");
      if (s == BT_OK && direct) {
        for (int i = 0; i < 2; i++) {
          printf("pivot it->en->it: %s\n   (it->en: %s)\n                -> %s\n", in[i], direct[i], out[i]);
          /* The second model ran: the result isn't the English it->en gave. */
          CHECK(strcmp(out[i], direct[i]) != 0, "pivot output is the it->en output: %s", out[i]);
        }
        printf("pivot, 2 sentences: %.0f ms\n", ms);
        bt_free_strings(out, 2);
      }
      if (direct) bt_free_strings(direct, 2);
      bt_free_string(error);
      error = NULL;
      bt_model_free(enit);
    }
  }

  bt_model_free(iten);
  bt_service_free(service);
  free(text);
  printf(failures ? "%d check(s) FAILED\n" : "all checks passed\n", failures);
  return failures ? 1 : 0;
}
