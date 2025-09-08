#include <argp.h>
#include "argparse.h"

const char *argp_program_version = "iclab-downloader 0.1.0";
const char *argp_program_bug_address = "abrar@andrew.cmu.edu";
char doc[] = "documentation for iclab-downloader";
char args_doc[] = "FILENAME";

struct argp_option opts[] = {
    {0}
};

error_t parse_opt(int key, char *arg, struct argp_state *state) {
    arguments_t *arguments = state -> input;

    switch(key) {
        case ARGP_KEY_ARG:
            // too many args
            if (state->arg_num > 1)
                argp_usage(state);

            arguments->args[state->arg_num] = arg;
            break;

        case ARGP_KEY_END:
            if (state->arg_num < 1)
                argp_usage(state);
            break;

        default:
            return ARGP_ERR_UNKNOWN;
    }

    return 0;
}

struct argp argp_struct = {
    opts,
    parse_opt,
    args_doc,
    doc,
    NULL,
    NULL,
    NULL,
};

/* ----- done argparse functions ----*/
