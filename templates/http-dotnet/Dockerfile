# The guest rootfs for {{name}}: the app, published self-contained and
# trimmed for linux-musl by the .NET SDK, on the published dotnet-aot base
# (exec driver, musl linker, glibc compat symlinks). `hluk build` builds it
# for the host's CPU and exports it as .hluk/rootfs.cpio; `hluk run` boots it.
FROM mcr.microsoft.com/dotnet/sdk:9.0-alpine@sha256:a4ef33907bcb0cf387a5e43fa9180667bf68d47527d5d219a9d8ee280c184130 AS build
WORKDIR /src
COPY Program.cs App.csproj ./
RUN dotnet publish -c Release --os linux-musl --self-contained \
    -p:PublishTrimmed=true \
    -p:InvariantGlobalization=true \
    -o /out

FROM {{base}}
COPY --from=build /out/ /app/
# The C++ runtime the CoreCLR host needs.
COPY --from=build /usr/lib/libstdc++.so.6 /usr/lib/libstdc++.so.6
COPY --from=build /usr/lib/libgcc_s.so.1 /usr/lib/libgcc_s.so.1
