# Makefile for ICLab downloader

BASEFLAGS = -g3 -Og
CFLAGS    = $(BASEFLAGS) -std=c11 -Werror -Wextra -Wall -Wpedantic \
            -Wstrict-prototypes -Wmissing-prototypes -Wwrite-strings \
            -Wconversion -Wno-unused-parameter
CPPFLAGS  = -I/app/curl/include
LFLAGS    = $(BASEFLAGS) -L/app/curl/lib
LIBS      = -lcurl -lncurses -ltinfo -largp

all: main
.PHONY: all clean run

main: main.o argparse.o progressbar.o
	$(CC) $(LFLAGS) -o downloader $^ $(LIBS) $(CFLAGS)

argparse.o: argparse.c argparse.h
main.o: main.c argparse.h progressbar.h

# not our code, I don't feel like fixing all the -Wconversion warnings
progressbar.o: progressbar.c progressbar.h
progressbar.o: CFLAGS += -Wno-conversion

clean:
	rm -f main *.o
	rm -f downloads/*.txt

run: all
	rm -f downloads/*.txt
	./main lists/urls.txt
