# Copyright (c) 2024 Bytedance Ltd. and/or its affiliates

#  Licensed under the Apache License, Version 2.0 (the "License");
#  you may not use this file except in compliance with the License.
#  You may obtain a copy of the License at

#      http://www.apache.org/licenses/LICENSE-2.0

#  Unless required by applicable law or agreed to in writing, software
#  distributed under the License is distributed on an "AS IS" BASIS,
#  WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#  See the License for the specific language governing permissions and
#  limitations under the License.

# Check if filename is provided as argument
if ($args.Count -ne 1) {
    Write-Host "Usage: .\pull_images.ps1 <image list file>"
    exit 1
}

# Read filename
$imageFile = $args[0]

# Check if file exists
if (-Not (Test-Path $imageFile)) {
    Write-Host "File $imageFile does not exist."
    exit 1
}

# Read file line by line and download images
Get-Content $imageFile | ForEach-Object {
    $image = $_.Trim()
    
    # Skip empty lines
    if (-Not [string]::IsNullOrWhiteSpace($image)) {
        Write-Host "Downloading image: $image"
        docker pull $image
    }
}

Write-Host "All images downloaded successfully."