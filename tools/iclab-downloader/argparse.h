#ifndef __ICLAB_ARGPARSE_H__
#define __ICLAB_ARGPARSE_H__

#include <argp.h>

extern char doc[];
extern char args_doc[];

extern struct argp_option opts[];

typedef struct arguments {
    char *args[1];
} arguments_t;

error_t parse_opt(int key, char *arg, struct argp_state *state);

extern struct argp argp_struct;

#endif
