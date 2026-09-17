/* Synthetic offline payload. No project or reviewer code is executed. */
#include <arpa/inet.h>
#include <fcntl.h>
#include <stddef.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <sys/un.h>
#include <unistd.h>

int main(int argc, char **argv) {
    if (argc != 5) return 1;
    if (getenv("GITHUB_TOKEN") || strcmp(getenv("HOME"), "/home")) return 2;
    if (access(argv[1], F_OK) == 0 || access("/escape", F_OK) == 0) return 3;
    if (open("/probe", O_WRONLY) >= 0 || open("/outside", O_CREAT|O_WRONLY, 0600) >= 0) return 4;
    if (access("/proc/1/environ", F_OK) == 0 || open("/dev/tty", O_RDWR) >= 0) return 5;
    if (fcntl(atoi(argv[4]), F_GETFD) >= 0) return 6;
    for (int fd = 0; fd < 3; fd++) if (isatty(fd)) return 7;
    int tcp = socket(AF_INET, SOCK_STREAM, 0);
    struct sockaddr_in remote = {.sin_family = AF_INET, .sin_port = htons(atoi(argv[2]))};
    inet_pton(AF_INET, "127.0.0.1", &remote.sin_addr);
    if (tcp >= 0 && connect(tcp, (struct sockaddr *)&remote, sizeof(remote)) == 0) return 8;
    close(tcp);
    int unix_socket = socket(AF_UNIX, SOCK_STREAM, 0);
    struct sockaddr_un address = {.sun_family = AF_UNIX};
    size_t length = strlen(argv[3]);
    if (length + 1 >= sizeof(address.sun_path)) return 9;
    memcpy(address.sun_path + 1, argv[3], length);
    if (unix_socket >= 0 && connect(unix_socket, (struct sockaddr *)&address,
            offsetof(struct sockaddr_un, sun_path) + length + 1) == 0) return 10;
    close(unix_socket);
    if (open("/home/session", O_CREAT|O_EXCL|O_WRONLY, 0600) < 0) return 11;
    puts("isolated");
    return 0;
}
