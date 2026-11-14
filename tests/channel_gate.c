/* Test-only syntax FIFO read gate. Never loaded by an ish adapter.
 *
 * Q token inode serial capacity\n -> A token inode serial length\n
 * Bad replies receive N\n; accepted replies receive G\n. Length zero means EOF.
 * Each grant covers one read chunk, NOT a command, physical line, or primary.
 * The controller writes that chunk to the FIFO before acknowledging it.
 * Cancellation after staging bytes requires retiring the FIFO, not reusing it.
 */
#define _GNU_SOURCE
#include <dlfcn.h>
#include <errno.h>
#include <limits.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <sys/un.h>
#include <unistd.h>

#define TOKEN_LIMIT 64
#define FRAME_LIMIT 256

static pid_t owner;
static char fifo_path[PATH_MAX];
static char socket_path[sizeof(((struct sockaddr_un *)0)->sun_path)];
static char token[TOKEN_LIMIT + 1];
static unsigned long long serial;
static ssize_t (*native_read)(int, void *, size_t);

static int copy_value(char *target, size_t capacity, const char *name) {
    const char *value = getenv(name);
    if (!value || !*value || strlen(value) >= capacity) return 0;
    strcpy(target, value);
    return 1;
}

__attribute__((constructor)) static void initialize(void) {
    native_read = dlsym(RTLD_NEXT, "read");
    if (native_read && copy_value(fifo_path, sizeof(fifo_path), "ISH_TEST_FIFO") &&
        copy_value(socket_path, sizeof(socket_path), "ISH_TEST_SOCKET") &&
        copy_value(token, sizeof(token), "ISH_TEST_TOKEN")) owner = getpid();
    /* Exec'd programs keep their ordinary input implementation. Forked children
     * are excluded by owner PID even before exec. This isolated test sets the
     * complete LD_PRELOAD value; it does not replace the production guard. */
    unsetenv("LD_PRELOAD");
    unsetenv("ISH_TEST_FIFO");
    unsetenv("ISH_TEST_SOCKET");
    unsetenv("ISH_TEST_TOKEN");
}

static int write_frame(int fd, const char *data) {
    size_t left = strlen(data);
    while (left) {
        ssize_t count = send(fd, data, left, MSG_NOSIGNAL);
        if (count <= 0) { if (!count) errno = EPIPE; return 0; }
        data += count;
        left -= (size_t)count;
    }
    return 1;
}

static int read_frame(int fd, char *frame) {
    size_t used = 0;
    while (used < FRAME_LIMIT - 1) {
        ssize_t count = native_read(fd, frame + used, 1);
        if (count != 1) { if (!count) errno = EPIPE; return 0; }
        if (frame[used++] == '\n') {
            frame[used - 1] = 0;
            return 1;
        }
    }
    errno = EPROTO;
    return 0;
}

ssize_t read(int fd, void *data, size_t capacity) {
    struct stat actual, registered;
    if (!native_read) native_read = dlsym(RTLD_NEXT, "read");
    if (!native_read) { errno = EIO; return -1; }
    if (!owner || owner != getpid() || !capacity ||
        fstat(fd, &actual) || !S_ISFIFO(actual.st_mode) || stat(fifo_path, &registered) ||
        actual.st_dev != registered.st_dev || actual.st_ino != registered.st_ino)
        return native_read(fd, data, capacity);

    struct sockaddr_un address = {.sun_family = AF_UNIX};
    strcpy(address.sun_path, socket_path);
    int control = socket(AF_UNIX, SOCK_STREAM | SOCK_CLOEXEC, 0);
    if (control < 0) return -1;
    if (connect(control, (struct sockaddr *)&address, sizeof(address))) {
        int error = errno;
        close(control);
        errno = error;
        return -1;
    }
    unsigned long long request = ++serial;
    unsigned long long inode = (unsigned long long)actual.st_ino;
    char frame[FRAME_LIMIT];
    snprintf(frame, sizeof(frame), "Q %s %llu %llu %zu\n", token, inode, request, capacity);
    if (!write_frame(control, frame)) goto broken;
    while (read_frame(control, frame)) {
        char candidate[TOKEN_LIMIT + 1], extra;
        unsigned long long candidate_inode, candidate_request;
        size_t length;
        int fields = sscanf(frame, "A %64s %llu %llu %zu %c", candidate,
                            &candidate_inode, &candidate_request, &length, &extra);
        if (fields != 4 || strcmp(candidate, token) || candidate_inode != inode ||
            candidate_request != request || length > capacity) {
            if (!write_frame(control, "N\n")) goto broken;
            continue;
        }
        if (!write_frame(control, "G\n")) goto broken;
        close(control);
        /* No credit survives this call. A short read needs a new request. */
        return length ? native_read(fd, data, length) : 0;
    }
broken:
    /* tcsh xread dispatches pending signals only when it sees EINTR. Do not
     * retry indefinitely here or turn a pending Ctrl+C into a generic I/O error. */
    {
        int error = errno;
        close(control);
        errno = error ? error : EIO;
        return -1;
    }
}
