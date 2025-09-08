#define _GNU_SOURCE 1
#define _FORTIFY_SOURCE 2
#undef NDEBUG
#define CURL_NO_OLDIES

#include <argp.h>
#include <assert.h>
#include <signal.h>
#include <stdarg.h>
#include <string.h>
#include <stdlib.h>
#include <stdio.h>
#include <unistd.h>

#include <curl/curl.h>
#include "argparse.h"
#include "progressbar.h"

#undef noreturn
#if __STDC_VERSION__ >= 202311L
#define noreturn [[noreturn]] void
#elif __STDC_VERSION__ >= 201112L
#define noreturn _Noreturn void
#elif defined __GNUC__ && __GNUC__ >= 3
#define noreturn void __attribute__ ((noreturn))
#else
#define noreturn void
#endif

/* Exit-on-error wrappers */
static void *xmalloc(size_t size) {
    void *p = malloc(size);
    if (!p) {
        fputs("fatal error: out of memory\n", stderr);
        exit(EXIT_FAILURE);
    }
    return p;
}

static char *xasprintf(const char *fmt, ...) {
    va_list ap;
    char *str = NULL;
    int rv;

    va_start(ap, fmt);
    rv = vasprintf(&str, fmt, ap);
    va_end(ap);
    if (rv < 0 || str == NULL) {
        fputs("fatal error: out of memory\n", stderr);
        exit(EXIT_FAILURE);
    }
    return str;
}

static noreturn curl_easy_failure(CURLcode res, const char *func) {
    fprintf(stderr, "%s failed: %s\n", func, curl_easy_strerror(res));
    exit(EXIT_FAILURE);
}

/* This has to be a macro because curl_easy_setopt's third argument is
   dynamically typed. */
#define x_curl_easy_setopt(x, y, z) do {                \
    CURLcode res_ = curl_easy_setopt((x), (y), (z));    \
    if (res_ != CURLE_OK) {                             \
        curl_easy_failure(res_, "curl_easy_setopt");    \
    }                                                   \
} while (0)

/* This has to be a macro because curl_easy_getinfo's third argument is
   dynamically typed. */
#define x_curl_easy_getinfo(x, y, z) do {                           \
    CURLcode res_ = curl_easy_getinfo((x), (y), (z));               \
    if (res_ != CURLE_OK) {                                         \
        curl_easy_failure(res_, "curl_easy_getinfo(" #y ")");       \
    }                                                               \
} while (0)

static noreturn curl_multi_failure(CURLMcode res, const char *func) {
    fprintf(stderr, "%s failed: %s\n", func, curl_multi_strerror(res));
    exit(EXIT_FAILURE);
}

static void x_curl_multi_remove_handle(CURLM *m, CURL *h) {
    CURLMcode res = curl_multi_remove_handle(m, h);
    if (res != CURLM_OK) {
        curl_multi_failure(res, "curl_multi_remove_handle");
    }
}

/* This has to be a macro because curl_multi_setopt's third argument is
   dynamically typed. */
#define x_curl_multi_setopt(x, y, z) do {               \
    CURLMcode res_ = curl_multi_setopt((x), (y), (z));  \
    if (res_ != CURLM_OK) {                             \
        curl_multi_failure(res_, "curl_multi_setopt");  \
    }                                                   \
} while (0)

/* These should be made into command line arguments */
static const char download_folder[] = "downloads";
static const long max_connections = 100;
static const long max_http2_connections = 200;
static const char user_agent[] =
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:121.0)"
    " Gecko/20100101 Firefox/121.0";

/* Signal handling */
static volatile sig_atomic_t pending_interrupt = 0;
static void sighandler(int signo) {
    pending_interrupt = 1;
}

/* Output buffering */
typedef struct response_buffer {
    FILE *stream;
    char *buffer;
    size_t bufsz;
} response_buffer;

static response_buffer *make_response_buffer(void) {
    response_buffer *b = xmalloc(sizeof(response_buffer));
    b->buffer = NULL;
    b->bufsz = 0;
    b->stream = open_memstream(&b->buffer, &b->bufsz);
    if (!b->stream) {
        perror("open_memstream");
        exit(EXIT_FAILURE);
    }
    return b;
}

static void free_response_buffer(response_buffer *b) {
    fclose(b->stream);
    free(b->buffer);
    free(b);
}

static CURL *make_request(char *url) {
    CURL *handle = curl_easy_init();
    if (!handle) {
        fputs("make_request: curl_easy_init failed\n", stderr);
        exit(EXIT_FAILURE);
    }

    /* Important: use HTTP2 over HTTPS */
    x_curl_easy_setopt(handle, CURLOPT_HTTP_VERSION, CURL_HTTP_VERSION_2TLS);
    x_curl_easy_setopt(handle, CURLOPT_URL, url);

    /* buffer body */
    response_buffer *buf = make_response_buffer();
    x_curl_easy_setopt(handle, CURLOPT_WRITEDATA, buf->stream);
    x_curl_easy_setopt(handle, CURLOPT_PRIVATE, buf);

    /* disable response compression */
    x_curl_easy_setopt(handle, CURLOPT_ACCEPT_ENCODING, "identity");
    /* For completeness */
    x_curl_easy_setopt(handle, CURLOPT_TIMEOUT_MS, 30000L);
    /* set useragent */
    x_curl_easy_setopt(handle, CURLOPT_USERAGENT, user_agent);

    /* disable certificate check  */
    x_curl_easy_setopt(handle, CURLOPT_SSL_VERIFYPEER, 0L);

    /* allow insecure ssl ciphers */
    x_curl_easy_setopt(handle, CURLOPT_SSL_CIPHER_LIST, "DEFAULT:!DH");
    return handle;
}

static char *strip_whitespace(char *line, size_t len) {
    while (len > 0
           && (line[len-1] == ' ' || line[len-1] == '\t'
               || line[len-1] == '\n' || line[len-1] == '\r')) {
        len -= 1;
    }
    if (len == 0)
        return NULL;
    line[len] = '\0';

    while (line[0] == ' ' || line[0] == '\t'
           || line[0] == '\n' || line[0] == '\r') {
        line += 1;
    }
    return line;
}

static size_t read_urls(char *filename, CURLM *multi_handle) {
    FILE *fp = fopen(filename, "r");
    if (fp == NULL) {
        perror(filename);
        exit(EXIT_FAILURE);
    }

    char *line = NULL;
    size_t len = 0;
    size_t n_urls = 0;
    for (;;) {
        ssize_t n_read = getline(&line, &len, fp);
        if (n_read < 0)
            break;
        assert(line);

        char *url = strip_whitespace(line, (size_t)n_read);
        if (!url || !url[0])
            continue;

        CURLMcode res = curl_multi_add_handle(multi_handle,
                                              make_request(url));
        if (res != CURLM_OK) {
            curl_multi_failure(res, "curl_multi_add_handle");
        }
        n_urls += 1;
    }
    free(line);
    fclose(fp);

    printf("Total number of requests: %zd\n", n_urls);
    return n_urls;
}

// Calls curl_global_init and curl_multi_init.
static CURLM *initialize_libcurl(void) {

    CURLcode ret = curl_global_init(CURL_GLOBAL_DEFAULT);
    if (ret != CURLE_OK) {
        curl_easy_failure(ret, "curl_global_init");
    }

    CURLM *multi_handle = curl_multi_init();
    if (!multi_handle) {
        fputs("initialize_libcurl: curl_multi_init failed\n", stderr);
        exit(EXIT_FAILURE);
    }

    x_curl_multi_setopt(multi_handle, CURLMOPT_MAX_TOTAL_CONNECTIONS,
                        max_connections);
    x_curl_multi_setopt(multi_handle, CURLMOPT_MAX_CONCURRENT_STREAMS,
                        max_http2_connections);

    // enable http2
#ifdef CURLPIPE_MULTIPLEX
    x_curl_multi_setopt(multi_handle, CURLMOPT_PIPELINING, CURLPIPE_MULTIPLEX);
#endif

    return multi_handle;
}

static void record_http_status(FILE *fp, CURL *handle) {
    long httpversion;
    x_curl_easy_getinfo(handle, CURLINFO_HTTP_VERSION, &httpversion);

    long response_code;
    x_curl_easy_getinfo(handle, CURLINFO_RESPONSE_CODE, &response_code);

    /* CURLINFO_HTTP_VERSION is *supposed* to return only one of
       the following subset of the CURL_HTTP_VERSION_* constants.
       The others are input only. */
    switch (httpversion) {
    case CURL_HTTP_VERSION_1_0:
        fprintf(fp, "HTTP/1.0 %ld\n", response_code);
        break;
    case CURL_HTTP_VERSION_1_1:
        fprintf(fp, "HTTP/1.1 %ld\n", response_code);
        break;
    case CURL_HTTP_VERSION_2:
        fprintf(fp, "HTTP/2 %ld\n", response_code);
        break;
    case CURL_HTTP_VERSION_3:
        fprintf(fp, "HTTP/3 %ld\n", response_code);
        break;
    case 0:  /* "version cannot be determined" */
        fprintf(fp, "HTTP/<undetermined> %ld\n", response_code);
        break;
    default:
        fprintf(fp, "[CURLINFO_HTTP_VERSION=%ld] %ld\n",
                httpversion, response_code);
        break;
    }
}

static void record_response_headers(FILE *fp, CURL *handle) {
    struct curl_header *h, *prev = NULL;
    for (;;) {
        h = curl_easy_nextheader(handle, CURLH_HEADER, -1, prev);
        if (!h)
            break;
        fprintf(fp, "%s: %s\n", h->name, h->value);
        prev = h;
    }
}

static void record_response_body(FILE *fp, response_buffer *mem) {
    fflush(mem->stream);
    if (mem->bufsz == 0)
        return;
    fputc('\n', fp);
    fwrite(mem->buffer, 1, mem->bufsz, fp);
}


static void finish_request(CURLMsg *msg, CURLM *multi_handle, int complete) {
    // write data to file with name downloads/<int>.txt
    char *filename = xasprintf("%s/%d.txt", download_folder, complete);
    FILE *fp = fopen(filename, "w");
    if (!fp) {
        perror(filename);
        exit(EXIT_FAILURE);
    }

    CURL *handle = msg->easy_handle;

    response_buffer *mem;
    x_curl_easy_getinfo(handle, CURLINFO_PRIVATE, &mem);

    char *url;
    x_curl_easy_getinfo(handle, CURLINFO_EFFECTIVE_URL, &url);

    fprintf(fp, "%s\n%s\n", url, curl_easy_strerror(msg->data.result));

    if (msg->data.result == CURLE_OK) {
        record_http_status(fp, handle);
        record_response_headers(fp, handle);
        record_response_body(fp, mem);
    }

    if (ferror(fp) || fclose(fp)) {
        perror(filename);
        exit(EXIT_FAILURE);
    }

    x_curl_multi_remove_handle(multi_handle, handle);
    curl_easy_cleanup(handle);
    free_response_buffer(mem);
    free(filename);
}

int main(int argc, char *args[]) {
    signal(SIGINT, sighandler);

    // argument parsing
    arguments_t arguments;
    argp_parse(&argp_struct, argc, args, 0, 0, &arguments);

    CURLM *multi_handle = initialize_libcurl();
    size_t pending = read_urls(arguments.args[0], multi_handle);
    int unused;
    int complete = 0;
    int still_running = 1;

    progressbar *bar = progressbar_new("Downloading", pending);
    assert(bar);

    while (still_running && !pending_interrupt) {
        int numfds;
        int res;

        res = curl_multi_wait(multi_handle, NULL, 0, 1000, &numfds);
        if (res != CURLM_OK) {
            curl_multi_failure(res, "curl_multi_wait");
        }

        res = curl_multi_perform(multi_handle, &still_running);
        if (res != CURLM_OK) {
            curl_multi_failure(res, "curl_multi_perform");
        }

        CURLMsg *msg;
        while((msg = curl_multi_info_read(multi_handle, &unused)) != NULL) {
            assert(msg->msg == CURLMSG_DONE);
            finish_request(msg, multi_handle, complete);
            progressbar_inc(bar);
            complete++;
            pending--;
        }
    }

    progressbar_finish(bar);
    curl_multi_cleanup(multi_handle);
    curl_global_cleanup();

    return 0;
}
