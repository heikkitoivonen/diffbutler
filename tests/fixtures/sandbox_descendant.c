/* Synthetic adversarial payload: orphan a child outside the initial session. */
#include <signal.h>
#include <stdio.h>
#include <string.h>
#include <sys/prctl.h>
#include <sys/types.h>
#include <sys/wait.h>
#include <unistd.h>

int main(int argc, char **argv) {
    if (argc != 3) return 1;
    int ready[2];
    if (pipe(ready) < 0) return 2;
    pid_t child = fork();
    if (child < 0) return 3;
    if (child == 0) {
        close(ready[0]);
        if (setsid() < 0) _exit(4);
        pid_t detached = fork();
        if (detached < 0) _exit(5);
        if (detached > 0) _exit(0);
        /* No terminal, pipe, or process-group lifetime can stand in for death. */
        for (int fd = 0; fd < 3; fd++) close(fd);
        if (prctl(PR_SET_NAME, argv[1], 0, 0, 0) < 0) _exit(6);
        if (write(ready[1], "!", 1) != 1) _exit(7);
        close(ready[1]);
        for (;;) pause();
    }
    close(ready[1]);
    char value;
    if (read(ready[0], &value, 1) != 1) return 8;
    close(ready[0]);
    if (waitpid(child, NULL, 0) != child) return 9;
    /* Give the host test time to acquire a pidfd before causing an exit. */
    sleep(1);
    if (!strcmp(argv[2], "success")) return 0;
    if (!strcmp(argv[2], "failure")) return 23;
    if (!strcmp(argv[2], "overflow")) {
        char output[4096];
        memset(output, 'x', sizeof(output));
        for (;;) if (write(1, output, sizeof(output)) < 0) return 10;
    }
    for (;;) pause();
}
